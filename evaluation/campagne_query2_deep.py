#!/usr/bin/env python3
"""Le profil deep_search elargi corrige-t-il les esquives residuelles ?

Meme modele que campagne_query2.py, meme jeu de remarques, meme
consigne, memes verdicts — mais chaque cas est joue DEUX fois sur
/api/query2 : deep_search=false (profil standard, reference) puis
deep_search=true (profil recherche approfondie : pools BM25/dense,
fusion, top_n reranking, couverture et quota elargis — section
query2.deep de settings.yaml).

CE QUE CE TEST MESURE EN PLUS, lu dans le code avant de l'ecrire :

  - la cle "iterative" porte le mode effectif ("standard"/"deep") et,
  - quand la recherche B se declenche, les parametres appliques
    (fusion_top_k, rerank_top_n, quota_par_requete) — le test peut
    verifier que le bras deep tourne reellement avec les replages
    elargis, pas en mode standard silencieux,
  - le nombre de passages soumis au modele (nb_passages) doit etre
    superieur ou egal en deep : un profil elargi qui rend moins de
    passages signale une regression de plomberie, pas un vrai bruit.

CE QUE CE TEST NE PEUT PAS FAIRE :

  - les deux bras partagent la meme structure fixe en 4 etapes : deep
    elargit l'amplitude des recherches, jamais la marche du pipeline,
  - une information absente de l'index reste invisible dans les deux
    bras (volet paysager non ingere, mesure le 15 aout 2026).

LECTURE DES RESULTATS : l'apport du mode deep se lit sur les cas ou le
bras standard esquive ou concede et ou le bras deep repond. Un cas qui
se degrade en deep (repond -> ESQUIVE) est le signal de bruit que ce
profil accepte en echange de la couverture — a arbitrer cas par cas.

DEUX BRAS, UN SEUL PASSAGE CHACUN : meme reserve que
campagne_query2 — un ecart d'un ou deux cas n'est interpretable
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
LABEL = os.environ.get("LABEL", "query2_deep")
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

# Consigne strictement identique a campagne_query2.py / campagne_consigne.py :
# la comparaison avec les campagnes anterieures n'a de sens qu'a consigne
# egale.
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

# Les bras comparent les deux profils du pipeline : standard (deep_search
# false) et recherche approfondie (deep_search true).
BRAS = (("standard", False), ("deep", True))


def poster(charge, timeout=1800):
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
        f"CAMPAGNE {LABEL} : pipeline iteratif, profil standard vs profil deep",
        "=" * 78,
        "route /api/query2   (deep_search actif depuis la PR en cours)",
        "Meme structure fixe en 4 etapes dans les deux bras : recherche A,",
        "analyse de couverture, recherche B a quota reserve si PARTIEL,",
        "generation finale. Seule difference entre les bras : l'amplitude",
        "de l'entonnoir de retrieval (section query2.deep de settings.yaml).",
        "Controle de plomberie : le bras deep doit rendre autant ou plus",
        "de passages au modele que le bras standard, cas par cas.",
        "Metrique principale : les esquives, pas le bon tome.",
        "Verdict de reference : verdict() a trois etats, importe de",
        "exporter_echanges. Le verdict naif est compte en parallele.",
        "",
    ]

    resultats = {}
    progression = {}  # cas -> dict de marqueurs d'amelioration/degradation
    with open(SORTIE, "w", encoding="utf-8") as sortie:
        for nom_bras, deep in BRAS:
            lignes += [f"BRAS {nom_bras.upper()}", "-" * 78]
            print(f"\n=== BRAS {nom_bras.upper()} (deep_search={deep}) ===")
            compte = {"esquive": 0, "esquive_naif": 0, "bon_tome": 0,
                      "bon_tome_tronque": 0, "mesurables": 0,
                      "couvert": 0, "partiel": 0, "recherche_b": 0,
                      "reformulees": 0, "mode_inattendu": 0}
            t_bras = time.time()
            for c in cas:
                charge = {
                    "query": c.get("remarque_mrae") or "",
                    "top_k": TOP_K,
                    "deep_search": deep,
                    "custom_prompt": CONSIGNE,
                }
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
                # ciblees, recherche B, reformulation, mode effectif.
                trace = res.get("iterative", {}) or {}
                couverture = trace.get("couverture", {}) or {}
                verdict_couv = couverture.get("verdict", "?")
                requetes_ciblees = couverture.get("requetes", []) or []
                recherche_b = trace.get("recherche_b", {}) or {}
                b_faite = bool(recherche_b.get("effectuee"))
                reformulation = trace.get("reformulation", {}) or {}
                question_reformulee = reformulation.get("question")
                mode_trace = trace.get("mode", "?")
                if mode_trace != nom_bras:
                    compte["mode_inattendu"] += 1
                if verdict_couv == "COUVERT":
                    compte["couvert"] += 1
                elif verdict_couv == "PARTIEL":
                    compte["partiel"] += 1
                compte["recherche_b"] += int(b_faite)
                reformulee = bool(
                    question_reformulee
                    and question_reformulee != reformulation.get("originale"))
                compte["reformulees"] += int(reformulee)

                # Progression cas par cas pour le croisement des bras.
                progression.setdefault(c["id"], {})[nom_bras] = {
                    "verdict": verdict_v,
                    "bon_tome": bon if attendus else None,
                    "nb_passages": len(passages),
                    "couverture": verdict_couv,
                }

                sortie.write(json.dumps({
                    "id": c["id"], "bras": nom_bras, "deep_search": deep,
                    "erreur": erreur,
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
                    "mode_trace": mode_trace,
                    "parametres_recherche_b": recherche_b.get("parametres"),
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
                          f"{len(passages):2}p/{len(sources):2}s "
                          f"mode={mode_trace}")
                if erreur:
                    marque += f"  ERREUR {erreur[:60]}"
                print(marque)
                lignes.append(marque)
            compte["duree_min"] = round((time.time() - t_bras) / 60, 1)
            resultats[nom_bras] = compte
            lignes.append("")

    # Croisement des bras, cas par cas.
    ameliores, degrades, passages_regresses, moins_de_passages = [], [], 0, 0
    for cid, p in progression.items():
        std, dp = p.get("standard"), p.get("deep")
        if not std or not dp:
            continue
        if std["verdict"] != dp["verdict"]:
            score = {"ESQUIVE": 0, "concede": 1, "repond": 2}
            delta = score.get(dp["verdict"], 1) - score.get(std["verdict"], 1)
            (ameliores if delta > 0 else degrades).append(
                f"    {cid}: {std['verdict']} -> {dp['verdict']}")
        if dp["nb_passages"] < std["nb_passages"]:
            passages_regresses += 1

    a = resultats.get("standard", {})
    b = resultats.get("deep", {})
    lignes += [
        "SYNTHESE", "-" * 78,
        f"{'':16}{'standard':>10}{'deep':>10}",
        f"{'esquives':16}{a.get('esquive', 0):>10}{b.get('esquive', 0):>10}   "
        f"sur {len(cas)}   (verdict a trois etats)",
        f"{'esquives naif':16}{a.get('esquive_naif', 0):>10}{b.get('esquive_naif', 0):>10}   "
        f"sur {len(cas)}   (ancien detecteur, pour memoire)",
        f"{'bon_tome':16}{a.get('bon_tome', 0):>10}{b.get('bon_tome', 0):>10}   "
        f"sur {a.get('mesurables', 0)} mesurables   (sur passages)",
        f"{'bon_tome tronq':16}{a.get('bon_tome_tronque', 0):>10}{b.get('bon_tome_tronque', 0):>10}   "
        f"sur {a.get('mesurables', 0)} mesurables   (sur sources[:10])",
        f"{'couvert':16}{a.get('couvert', 0):>10}{b.get('couvert', 0):>10}",
        f"{'partiel':16}{a.get('partiel', 0):>10}{b.get('partiel', 0):>10}   "
        f"(recherche B declenchee)",
        f"{'recherche B':16}{a.get('recherche_b', 0):>10}{b.get('recherche_b', 0):>10}",
        f"{'reformulees':16}{a.get('reformulees', 0):>10}{b.get('reformulees', 0):>10}   "
        f"(demandes transformees en question)",
        f"{'mode inattendu':16}{a.get('mode_inattendu', 0):>10}{b.get('mode_inattendu', 0):>10}   "
        f"(trace.mode != bras : plomberie a revoir)",
        f"{'duree (min)':16}{a.get('duree_min', 0):>10}{b.get('duree_min', 0):>10}",
        "",
        "CROISEMENT STANDARD -> DEEP, cas par cas :",
        f"  ameliores ({len(ameliores)}) :",
    ]
    lignes += ameliores or ["    (aucun)"]
    lignes += [f"  degrades ({len(degrades)}) :"]
    lignes += degrades or ["    (aucun)"]
    lignes += [
        "",
        f"Controle de plomberie : {passages_regresses} cas rendent MOINS",
        "de passages en deep qu'en standard (attendu : 0 — sinon le",
        "profil elargi n'est pas reellement applique).",
        "",
        "Lecture : un cas ESQUIVE en standard qui passe a repond/concede",
        "en deep valide le profil elargi. Un cas degrade (repond ->",
        "ESQUIVE) est le cout du bruit accepte : examiner ses passages",
        "dans le jsonl avant de conclure a une regression.",
        "",
        "Rappel : les cas a reference vide (esquive legitime) doivent",
        "rester en esquive dans les deux bras ; une reponse inventee",
        "serait une faute.",
    ]

    texte = "\n".join(lignes)
    print("\n" + "\n".join(lignes[-30:]))
    with open(RAPPORT, "w", encoding="utf-8") as f:
        f.write(texte + "\n")
    print(f"\nEcrit : {SORTIE}\nEcrit : {RAPPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
