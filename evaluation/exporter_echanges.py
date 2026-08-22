#!/usr/bin/env python3
"""Exporte les echanges du chat pour transmission et relecture.

POURQUOI CE SCRIPT EXISTE

Rien n'a besoin d'etre ajoute au produit pour partager ce que rend
l'interface : tout est deja enregistre. Lu dans rag-system/src/agent/api.py
au commit 7dbdbac, avant d'ecrire une ligne de ce fichier :

  ligne 432  QUERY_HISTORY_DB, defaut /app/feedbacks/ragas.db
  ligne 438  table query_history : timestamp, question, answer, contexts,
             index_name, processing_time_ms. Alimentee par _log_query a
             chaque passage par le pipeline procedural.
  ligne 452  table agent_runs : question, answer, sources, trace,
             steps_used, stopped_reason, escalated. Alimentee par
             _log_agent_run a chaque passage par la boucle agentique.
  ligne 1551 FEEDBACK_DB_PATH, defaut /app/feedbacks/feedbacks.db, table
             feedbacks : query, response, sources, feedback up ou down,
             expected_response, comment.

Consequence utile : la table dans laquelle un echange atterrit dit quelle
route l'a produit. query_history pour le pipeline, agent_runs pour la
boucle. Aucune supposition n'est necessaire.

CE QUE PRODUIT CE SCRIPT

  echanges_<LABEL>.jsonl  une ligne par echange, texte integral, la route
                          d'origine, la duree, les sources, le verdict,
                          et le retour associe s'il en existe un.
  rapport_<LABEL>.txt     un tableau lisible et le decompte des verdicts.

Les contextes, souvent volumineux, ne sont pas recopies en entier : seuls
leur nombre et leur longueur totale sont conserves, sauf si CONTEXTES=1.

VERDICTS

Meme detecteur a trois etats que les campagnes, de facon que ce que tu
liras ici se compare directement a ce qui a ete mesure les 5 et 6 aout :
esquive si l'absence est annoncee dans les quinze premiers pour cent du
texte, ou si la reponse fait moins de sept cents caracteres, ou si elle ne
cite aucun document. Concession si l'absence est posee apres la substance.
Sinon reponse nette. Seule l'esquive est un echec.

USAGE

  docker exec luciole-agent-mrae python /app/evaluation/exporter_echanges.py

  DEPUIS=2026-08-06       ne prend que les echanges a partir de cette date
  DERNIERS=20            ne prend que les vingt plus recents
  LABEL=essai_ui         prefixe des fichiers ecrits
  CONTEXTES=1            recopie les passages transmis au modele
"""
import json
import os
import re
import sqlite3
import sys

BASE = os.environ.get("EVAL_DIR", "/app/evaluation")
RAGAS_DB = os.environ.get("QUERY_HISTORY_DB", "/app/feedbacks/ragas.db")
FEEDBACK_DB = os.environ.get("FEEDBACK_DB_PATH", "/app/feedbacks/feedbacks.db")
LABEL = os.environ.get("LABEL", "ui")
DEPUIS = os.environ.get("DEPUIS", "").strip()
DERNIERS = int(os.environ.get("DERNIERS", "0"))
CONTEXTES = os.environ.get("CONTEXTES", "0") == "1"

SORTIE = os.path.join(BASE, f"echanges_{LABEL}.jsonl")
RAPPORT = os.path.join(BASE, f"rapport_{LABEL}.txt")

# Singulier ET pluriel. Le motif d'origine ne couvrait que le pluriel :
# "le dossier ne mentionne pas" passait pour une reponse.
ESQUIVE = re.compile(
    # ne / n' + <verbe> + pas / aucun, aux deux nombres
    r"n(?:e |')(?:contien(?:t|nent)|mentionn(?:e|ent)|fourni(?:t|ssent)|"
    r"permet(?:tent)?|cite(?:nt)?|precise(?:nt)?|indique(?:nt)?|"
    r"detaille(?:nt)?|comporte(?:nt)?|evoque(?:nt)?|abord(?:e|ent)) "
    r"(?:pas|aucun)|"
    # n'en parle(nt) pas
    r"n'en parle(?:nt)? pas|"
    # n'est / ne sont pas mentionne, precise, indique, detaille, aborde
    r"(?:n'est|ne sont) pas (?:explicitement )?"
    r"(?:mentionn|precis|indiqu|detaill|abord)|"
    # tournures nominales
    r"aucune information|aucune mention|aucune precision|aucun element|"
    r"pas d'information|pas de mention|pas de precision|"
    # absence dite autrement
    r"reste(?:nt)? muet|est absente? d|sont absentes? d", re.I)
# Le souligne n'est pas obligatoire : le modele ecrit "Tome 4, page 1"
# aussi souvent que "tome_4.pdf". Exiger tome_\d faisait passer une
# reponse correctement sourcee pour une reponse sans source.
# Motif elargi (mesure Beaumont Sud, cas beaumont-11, 22 aout 2026) : le
# pipeline query2 cite parfois les sources par leur intitule de volet
# plutot que par "Tome N" ou un nom de fichier .pdf, ex. "Volet
# environnement naturel, p. 460" ou "RNT, p. 62". L'ancien motif ne les
# reconnaissait pas, ce qui comptait a tort une reponse sourcee comme
# non sourcee et forcait un verdict ESQUIVE via la clause SOURCE_CITEE
# de verdict() ci-dessous.
SOURCE_CITEE = re.compile(
    r"\.pdf|tome[_ ]?\d|\[?source\s*:|"
    r"volet\s+(?:environnement|milieu|paysage)|\bRNT\b|p\.\s*\d+", re.I)


def verdict(texte):
    if not texte:
        return "ESQUIVE"
    m = ESQUIVE.search(texte)
    if not m:
        return "repond"
    if m.start() / len(texte) < 0.15 or len(texte) < 700 \
            or not SOURCE_CITEE.search(texte):
        return "ESQUIVE"
    return "concede"


def colonnes(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def tables(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def lire_json(valeur):
    if not valeur:
        return []
    try:
        v = json.loads(valeur)
        return v if isinstance(v, list) else [v]
    except (ValueError, TypeError):
        return []


def noms_de_sources(sources):
    noms = []
    for s in sources:
        if isinstance(s, dict):
            n = (s.get("file_name") or s.get("filename")
                 or (s.get("metadata") or {}).get("file_name"))
            if n:
                noms.append(n)
        elif isinstance(s, str):
            noms.append(s)
    return noms


def charger_retours():
    """query, response tronquee -> retour. Rapproche par la question."""
    if not os.path.exists(FEEDBACK_DB):
        return {}
    par_question = {}
    try:
        with sqlite3.connect(FEEDBACK_DB) as conn:
            conn.row_factory = sqlite3.Row
            if "feedbacks" not in tables(conn):
                return {}
            for r in conn.execute(
                    "SELECT query, feedback, comment, expected_response, "
                    "timestamp FROM feedbacks ORDER BY id"):
                par_question[(r["query"] or "").strip()] = {
                    "retour": r["feedback"],
                    "commentaire": r["comment"],
                    "reponse_attendue": r["expected_response"],
                    "horodatage_retour": r["timestamp"],
                }
    except sqlite3.Error as e:
        print(f"  base des retours illisible : {e}")
    return par_question


def charger_echanges():
    lignes = []
    if not os.path.exists(RAGAS_DB):
        print(f"  base introuvable : {RAGAS_DB}")
        return lignes
    with sqlite3.connect(RAGAS_DB) as conn:
        conn.row_factory = sqlite3.Row
        presentes = tables(conn)

        if "query_history" in presentes:
            for r in conn.execute(
                    "SELECT * FROM query_history ORDER BY id"):
                contextes = lire_json(r["contexts"])
                lignes.append({
                    "route": "pipeline",
                    "horodatage": r["timestamp"],
                    "question": r["question"],
                    "reponse": r["answer"] or "",
                    "index": r["index_name"],
                    "duree_s": round((r["processing_time_ms"] or 0) / 1000, 1),
                    "nb_contextes": len(contextes),
                    "car_contextes": sum(len(str(c)) for c in contextes),
                    "sources": [],
                    "trace": None,
                    "contextes": contextes if CONTEXTES else None,
                })

        if "agent_runs" in presentes:
            cols = colonnes(conn, "agent_runs")
            for r in conn.execute("SELECT * FROM agent_runs ORDER BY id"):
                sources = lire_json(r["sources"] if "sources" in cols else None)
                lignes.append({
                    "route": "agent",
                    "horodatage": r["timestamp"],
                    "question": r["question"],
                    "reponse": r["answer"] or "",
                    "index": r["index_name"] if "index_name" in cols else None,
                    "duree_s": round(
                        (r["processing_time_ms"] or 0) / 1000, 1)
                    if "processing_time_ms" in cols else None,
                    "nb_contextes": len(sources),
                    "car_contextes": sum(len(str(s)) for s in sources),
                    "sources": noms_de_sources(sources),
                    "profil": r["profile_name"] if "profile_name" in cols else None,
                    "pas_utilises": r["steps_used"] if "steps_used" in cols else None,
                    "raison_arret": r["stopped_reason"] if "stopped_reason" in cols else None,
                    "escalade": bool(r["escalated"]) if "escalated" in cols else None,
                    "trace": (r["trace"] if "trace" in cols else None)
                    if CONTEXTES else None,
                    "contextes": None,
                })

    lignes.sort(key=lambda x: x["horodatage"] or "")
    if DEPUIS:
        lignes = [x for x in lignes if (x["horodatage"] or "") >= DEPUIS]
    if DERNIERS:
        lignes = lignes[-DERNIERS:]
    return lignes


def main():
    retours = charger_retours()
    echanges = charger_echanges()
    if not echanges:
        print("Aucun echange a exporter. "
              "Pose au moins une question dans l'interface.")
        return 1

    for x in echanges:
        x["verdict"] = verdict(x["reponse"])
        x["longueur_reponse"] = len(x["reponse"])
        x["cite_un_document"] = bool(SOURCE_CITEE.search(x["reponse"]))
        x["pages_citees"] = len(re.findall(
            r"pages?\s*(?:\d+|vari)", x["reponse"], re.I))
        fb = retours.get((x["question"] or "").strip())
        x["retour"] = fb or None

    with open(SORTIE, "w", encoding="utf-8") as f:
        for x in echanges:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")

    compte = {}
    par_route = {}
    for x in echanges:
        compte[x["verdict"]] = compte.get(x["verdict"], 0) + 1
        d = par_route.setdefault(x["route"], {})
        d[x["verdict"]] = d.get(x["verdict"], 0) + 1

    lignes = [
        f"ECHANGES EXPORTES : {LABEL}",
        "=" * 78,
        f"  base des echanges : {RAGAS_DB}",
        f"  base des retours  : {FEEDBACK_DB}",
        f"  echanges exportes : {len(echanges)}",
        f"  filtre depuis     : {DEPUIS or 'aucun'}",
        f"  derniers retenus  : {DERNIERS or 'tous'}",
        "",
        "La table d'origine dit la route empruntee : query_history pour le",
        "pipeline procedural, agent_runs pour la boucle agentique. Un meme",
        "echange n'apparait jamais dans les deux.",
        "",
        "TABLEAU",
        "-" * 78,
        f"{'horodatage':20}{'route':10}{'verdict':9}{'car.':>7}{'ctx':>5}"
        f"{'duree':>8}{'pg':>4}{'fb':>6}  question",
    ]
    for x in echanges:
        q = (x["question"] or "").replace("\n", " ")
        lignes.append(
            f"{(x['horodatage'] or '')[:19]:20}{x['route']:10}"
            f"{x['verdict']:9}{x['longueur_reponse']:>7}{x['nb_contextes']:>5}"
            f"{(x['duree_s'] if x['duree_s'] is not None else 0):>8.1f}"
            f"{x['pages_citees']:>4}"
            f"{(x['retour']['retour'] if x['retour'] else '-'):>6}  "
            f"{q[:60]}")

    lignes += [
        "",
        "DECOMPTE",
        "-" * 78,
        "  tous echanges : " + " ".join(
            f"{k}={compte.get(k, 0)}"
            for k in ("repond", "concede", "ESQUIVE")),
    ]
    for route, d in sorted(par_route.items()):
        lignes.append(f"  {route:13} : " + " ".join(
            f"{k}={d.get(k, 0)}" for k in ("repond", "concede", "ESQUIVE")))

    avec_retour = [x for x in echanges if x["retour"]]
    lignes += [
        "",
        f"  echanges avec un retour saisi : {len(avec_retour)}/{len(echanges)}",
    ]
    for x in avec_retour:
        r = x["retour"]
        lignes.append(f"      {r['retour']:5} {(x['question'] or '')[:52]}")
        if r.get("commentaire"):
            lignes.append(f"            commentaire : {r['commentaire'][:60]}")

    lignes += [
        "",
        "REPERES MESURES, memes vingt remarques du jeu mrae",
        "-" * 78,
        "  bras                        esq  conc  net  bon_tome  duree",
        "  question brute + consigne     0     3   17     9/10   14.3 min",
        "  reformulation v2 + consigne   1     4   15    10/10   29.1 min",
        "  profil agent complet : 8 bon_tome, 2 mauvais, 4 sans reponse",
        "",
        "  Plancher de bruit mesure le 6 aout sur le premier bras :",
        "  19 reponses identiques au caractere sur 20 entre deux passages,",
        "  zero verdict change. Un ecart d'un cas est donc un fait.",
        "",
        "  L'esquive residuelle du second bras porte sur une remarque qui",
        "  interroge la forme du dossier, la lisibilite de ses cartes, et",
        "  qu'aucun corpus ne peut renseigner.",
    ]

    texte = "\n".join(lignes)
    print(texte)
    with open(RAPPORT, "w", encoding="utf-8") as f:
        f.write(texte + "\n")
    print(f"\nEcrit : {SORTIE}\nEcrit : {RAPPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
