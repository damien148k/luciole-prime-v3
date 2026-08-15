#!/usr/bin/env python3
"""Le pipeline iteratif query2 corrige-t-il les esquives residuelles ?

Meme modele que campagne_consigne.py, meme jeu de remarques, meme
consigne, memes verdicts — mais la route testee est /api/query2, le
pipeline iteratif : recherche A classique, analyse de couverture,
recherche B a quota reserve si PARTIEL.

CE QUE CE TEST MESURE EN PLUS, lu dans le code avant de l'ecrire :

  - la reponse de /api/query2 porte une cle "iterative" : verdict de
    couverture (COUVERT/PARTIEL), requetes ciblees proposees, trace de
    la recherche B (effectuee, proteges, passages finaux) et la
    reformulation eventuelle de la demande en question directe,
  - la propriete de securite du pipeline : si COUVERT, la reponse est
    STRICTEMENT celle de la route classique — les cas COUVERT de cette
    campagne doivent donc reproduire les verdicts de campagne_consigne,
    tout l'apport se lit sur les cas PARTIEL,
  - plafond dur cote serveur : 1 round, 2 recherches max. Chaque PARTIEL
    coute la recherche B : la duree par cas double environ.

CE QUE CE TEST NE PEUT PAS FAIRE :

  - enable_rewriting et deep_search sont ignores par l'endpoint
    (docstring de iterative_query, api.py) : le QueryRewriter et ses
    synonymes ne s'appliquent pas sur cette route,
  - la recherche B ne peut trouver que ce que la recherche A aurait pu
    trouver avec un meilleur vocabulaire : un tome absent de l'index
    reste invisible (mesure le 15 aout 2026 : volet paysager manquant a
    l'index, verdict COUVERT correct au vu du corpus indexe).

DEUX BRAS, UN SEUL PASSAGE CHACUN : meme reserve que
campagne_consigne — un ecart d'un ou deux cas n'est interpretable
qu'apres remesure du plancher de bruit (campagne_reproductibilite.py).
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# Nom du champ du jeu de questions qui porte les references attendues.
# Surchargeable pour un jeu construit avant le renommage :
#   docker exec -e CHAMP_REFERENCE=<ancien_nom> ...
CHAMP_REFERENCE = os.environ.get("CHAMP_REFERENCE", "sources_citees_reference")

BASE = os.environ.get("EVAL_DIR", "/app/evaluation")
# Jeu de test fourni par l'instance, jamais versionne : il porte les
# remarques d'un dossier reel. Surchargeable pour un autre dossier,
# par exemple JEU=/app/evaluation/jeu_test_mon_dossier.jsonl
JEU = os.environ.get("JEU", os.path.join(BASE, "jeu_test_mrae.jsonl"))
API = os.environ.get("LUCIOLE_API", "http://localhost:8000")
LABEL = os.environ.get("LABEL", "query2")
TOP_K = int(os.environ.get("TOP_K", "30"))

SORTIE = os.path.join(BASE, f"campagne_{LABEL}.jsonl")
RAPPORT = os.path.join(BASE, f"rapport_{LABEL}.txt")

sys.path.insert(0, BASE)
from diag_rappel_pages import references_attendues, tome_du_nom  # noqa: E402
# Detecteur de reference, defini une seule fois. Trois etats :
#   ESQUIVE  absence annoncee dans les 15 premiers % du texte, ou
#            reponse < 700 caracteres, ou aucune source citee
#   concede  absence posee apres la substance
#   repond   aucune annonce d'absence
from exporter_echanges import verdict as verdict_reference  # noqa: E402

# Consigne strictement identique a campagne_consigne.py : la comparaison
# des bras entre les deux campagnes n'a de sens qu'a consigne egale.
CONSIGNE = """CONTEXTE DE TRAVAIL

La question qui t'est posee est une observation de l'autorite
environnementale portant SUR le dossier d'etude d'impact. Ce n'est pas un
element a retrouver DANS le dossier. Il est normal que cette observation
n'y figure pas : elle a ete ecrite apres, en reaction a lui.

CE QU'IL FAUT PRODUIRE

Reponds sur le fond du point souleve, en rassemblant dans les extraits ce
que le dossier apporte deja sur ce sujet. Ta reponse doit pouvoir servir
de base a un memoire en reponse adresse a l'autorite environnementale.

CE QUI EST INTERDIT

N'ecris jamais que les documents ne mentionnent pas la recommandation, ne
la citent pas, ou n'en parlent pas. Cette phrase est toujours vraie et
n'a aucune valeur pour le lecteur.

SI LE DOSSIER NE COUVRE PAS LE POINT

Expose d'abord ce qu'il contient de plus proche, avec ses sources, puis
indique en une phrase le point precis qui reste a completer. Ne reponds
jamais par le seul constat d'une absence.
"""

ESQUIVE = re.compile(
    r"ne contiennent (aucune|pas)|aucune information|n'est pas (explicitement )?"
    r"mentionn|ne mentionnent pas|ne fournissent pas|pas d'information|"
    r"ne permettent pas|ne citent pas|n'en parlent pas", re.I)


def poster(charge, timeout=900):
    donnees = json.dumps(charge, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        API + "/api/query2", data=donnees,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def tomes_des_sources(sources):
    noms = [s.get("file_name") or s.get("filename") or "" for s in sources]
    return {t for t in (tome_du_nom(n) for n in noms) if t is not None}


def main():
    cas = [json.loads(x) for x in open(JEU, encoding="utf-8") if x.strip()]
    lignes = [
        f"CAMPAGNE {LABEL} : pipeline iteratif, avec et sans consigne",
        "=" * 78,
        "route /api/query2   (enable_rewriting et deep_search ignores)",
        "Recherche A identique a la route classique, puis analyse de",
        "couverture sur passages complets ; recherche B a quota reserve",
        "uniquement si PARTIEL. Plafond serveur : 1 round, 2 recherches.",
        "Propriete de securite : un cas COUVERT retourne la reponse de",
        "la route classique a l'identique — l'apport se lit sur PARTIEL.",
        "Seule difference entre les deux bras : la consigne.",
        "Metrique principale : les esquives, pas le bon tome.",
        "Verdict de reference : verdict() a trois etats, importe de",
        "exporter_echanges. Le verdict naif est compte en parallele.",
        "",
    ]

    resultats = {}
    with open(SORTIE, "w", encoding="utf-8") as sortie:
        for bras in ("sans", "avec"):
            lignes += [f"BRAS {bras.upper()}", "-" * 78]
            print(f"\n=== BRAS {bras.upper()} ===")
            compte = {"esquive": 0, "esquive_naif": 0, "bon_tome": 0,
                      "bon_tome_tronque": 0, "mesurables": 0,
                      "couvert": 0, "partiel": 0, "recherche_b": 0,
                      "reformulees": 0}
            t_bras = time.time()
            for c in cas:
                charge = {
                    "query": c.get("remarque_mrae") or "",
                    "top_k": TOP_K,
                }
                if bras == "avec":
                    charge["custom_prompt"] = CONSIGNE
                t0 = time.time()
                erreur = None
                try:
                    res = poster(charge)
                except (urllib.error.URLError, OSError, ValueError) as err:
                    res, erreur = {}, f"{type(err).__name__}: {err}"
                duree = time.time() - t0
                reponse = res.get("response", "") or ""
                sources = res.get("sources", []) or []
                passages = res.get("passages", []) or []
                # res["sources"] est tronque a dix par
                # _extract_sources ; res["passages"] porte les trente
                # entrees reellement soumises au modele. Le score se
                # calcule sur passages ; sources reste mesure pour
                # rendre l'ecart visible.
                tomes_tronques = tomes_des_sources(sources)
                tomes = tomes_des_sources(passages) or tomes_tronques
                attendus = {r[0] for r in
                            references_attendues(c.get(CHAMP_REFERENCE))}
                verdict_v = verdict_reference(reponse)
                esq = verdict_v == "ESQUIVE"
                esq_naif = bool(ESQUIVE.search(reponse))
                bon = bool(attendus and (tomes & attendus))
                bon_tronque = bool(attendus and (tomes_tronques & attendus))
                if attendus:
                    compte["mesurables"] += 1
                    compte["bon_tome"] += int(bon)
                    compte["bon_tome_tronque"] += int(bon_tronque)
                compte["esquive"] += int(esq)
                compte["esquive_naif"] += int(esq_naif)

                # Trace iterative : verdict de couverture, requetes
                # ciblees, recherche B, reformulation.
                trace = res.get("iterative", {}) or {}
                couverture = trace.get("couverture", {}) or {}
                verdict_couv = couverture.get("verdict", "?")
                requetes_ciblees = couverture.get("requetes", []) or []
                recherche_b = trace.get("recherche_b", {}) or {}
                b_faite = bool(recherche_b.get("effectuee"))
                reformulation = trace.get("reformulation", {}) or {}
                question_reformulee = reformulation.get("question")
                if verdict_couv == "COUVERT":
                    compte["couvert"] += 1
                elif verdict_couv == "PARTIEL":
                    compte["partiel"] += 1
                compte["recherche_b"] += int(b_faite)
                reformulee = bool(
                    question_reformulee
                    and question_reformulee != reformulation.get("originale"))
                compte["reformulees"] += int(reformulee)

                sortie.write(json.dumps({
                    "id": c["id"], "bras": bras, "erreur": erreur,
                    "verdict": verdict_v,
                    "esquive": esq, "esquive_naif": esq_naif,
                    "bon_tome": bon if attendus else None,
                    "bon_tome_tronque": bon_tronque if attendus else None,
                    "duree_s": round(duree, 1),
                    "longueur_reponse": len(reponse),
                    "tomes_rendus": sorted(tomes),
                    "tomes_rendus_tronques": sorted(tomes_tronques),
                    "nb_sources": len(sources),
                    "nb_passages": len(passages),
                    "couverture": verdict_couv,
                    "requetes_ciblees": requetes_ciblees,
                    "recherche_b": b_faite,
                    "question_reformulee": question_reformulee,
                    "reponse": reponse,
                }, ensure_ascii=False) + "\n")
                sortie.flush()
                marque = (f"  {c['id']:9} "
                          f"{verdict_v:<8} "
                          f"{'naif:ESQ' if esq_naif else '        '} "
                          f"{verdict_couv:<8} "
                          f"{'B' if b_faite else ' '} "
                          f"{'bon_tome' if bon else '        '} "
                          f"{'(perdu si tronque)' if bon and not bon_tronque else '':<18} "
                          f"{duree:5.1f}s  {len(reponse):5} car.  "
                          f"{len(passages):2}p/{len(sources):2}s")
                if erreur:
                    marque += f"  ERREUR {erreur[:60]}"
                print(marque)
                lignes.append(marque)
            compte["duree_min"] = round((time.time() - t_bras) / 60, 1)
            resultats[bras] = compte
            lignes.append("")

    a, b = resultats["sans"], resultats["avec"]
    lignes += [
        "SYNTHESE", "-" * 78,
        f"{'':14}{'sans':>10}{'avec':>10}",
        f"{'esquives':14}{a['esquive']:>10}{b['esquive']:>10}   "
        f"sur {len(cas)}   (verdict a trois etats)",
        f"{'esquives naif':14}{a['esquive_naif']:>10}{b['esquive_naif']:>10}   "
        f"sur {len(cas)}   (ancien detecteur, pour memoire)",
        f"{'bon_tome':14}{a['bon_tome']:>10}{b['bon_tome']:>10}   "
        f"sur {a['mesurables']} mesurables   (sur passages)",
        f"{'bon_tome tronq':14}{a['bon_tome_tronque']:>10}{b['bon_tome_tronque']:>10}   "
        f"sur {a['mesurables']} mesurables   (sur sources[:10])",
        f"{'couvert':14}{a['couvert']:>10}{b['couvert']:>10}   "
        f"(reponse identique a la route classique)",
        f"{'partiel':14}{a['partiel']:>10}{b['partiel']:>10}   "
        f"(recherche B declenchee)",
        f"{'recherche B':14}{a['recherche_b']:>10}{b['recherche_b']:>10}",
        f"{'reformulees':14}{a['reformulees']:>10}{b['reformulees']:>10}   "
        f"(demandes transformees en question)",
        f"{'duree (min)':14}{a['duree_min']:>10}{b['duree_min']:>10}",
        "",
        "Lecture : l'apport de query2 se mesure sur les cas PARTIEL —",
        "une esquive de la route classique qui devient repond/concede",
        "valide le pipeline. Une esquive qui survit a PARTIEL signifie",
        "que la recherche B n'a pas trouve mieux : le correctif est",
        "alors cote retrieval (vocabulaire des requetes ciblees) ou",
        "cote generation, pas cote verdict de couverture.",
        "",
        "Controle d'integrite : les cas COUVERT doivent rendre les",
        "memes verdicts que campagne_consigne sur le meme jeu — tout",
        "ecart y est un signal de bruit de mesure, pas un effet reel.",
        "",
        "Rappel : les cas a reference vide (esquive legitime) doivent",
        "rester en esquive dans les deux bras ; un PARTIEL sur ces cas",
        "est attendu et sain, une reponse inventee serait une faute.",
    ]

    texte = "\n".join(lignes)
    print("\n" + "\n".join(lignes[-22:]))
    with open(RAPPORT, "w", encoding="utf-8") as f:
        f.write(texte + "\n")
    print(f"\nEcrit : {SORTIE}\nEcrit : {RAPPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
