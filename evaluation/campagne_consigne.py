#!/usr/bin/env python3
"""La consigne de generation corrige-t-elle l'ecart de registre ?

Le defaut vise : Luciole lit la remarque d'une autorite environnementale
comme une question documentaire, cherche la recommandation DANS l'etude
d'impact, ne l'y trouve pas, et repond qu'elle n'y est pas. C'est exact et
hors sujet : la recommandation porte sur le dossier, elle n'est pas dedans.

Ce script fait tourner le jeu de questions deux fois, sans consigne puis
avec, tout le reste identique.

METRIQUE PRINCIPALE : le nombre d'esquives, pas le bon tome.

CE QUE CE TEST NE PEUT PAS FAIRE, lu dans le code avant de l'ecrire :

  - OptionsModel (src/agent/api.py) n'a que detail_level, max_items et
    include_sources. /api/analyze ne peut donc PAS porter de
    custom_prompt. La route utilisee ici est /api/query2, qui expose ce
    champ,
  - le champ top_k de la requete ne regle pas la profondeur de recherche :
    api.py le convertit en options["max_items"], que _analyze_chat ne
    recoit pas. La profondeur vient de
    LIMITS["standard"]["max_total_chunks"] = 100,
  - llm.py, _build_system_prompt AJOUTE la consigne au prompt de base, il
    ne le remplace pas,
  - llm.py, _format_rag_prompt termine le message utilisateur par "Reponds
    en t'appuyant exclusivement sur les extraits ci-dessus. Si
    l'information n'est pas presente, dis-le clairement." Cette phrase est
    ecrite en dur et produit exactement le comportement qu'on essaie de
    corriger. La consigne entre donc en concurrence avec elle.

Si le bras avec consigne ne bouge pas, la conclusion n'est pas que la
consigne est mauvaise : c'est que le correctif doit se faire dans
_format_rag_prompt, donc en code.

DEUX BRAS, UN SEUL PASSAGE CHACUN : a verifier avant de s'y fier. Le
plancher de bruit doit etre remesure sur le pipeline corrige avec
campagne_reproductibilite.py. Tant que ce n'est pas fait, un ecart de un
ou deux cas entre les bras n'est pas interpretable.
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
LABEL = os.environ.get("LABEL", "consigne")
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
        f"CAMPAGNE {LABEL} : consigne de generation, avec et sans",
        "=" * 78,
        "route /api/query2",
        "Le pipeline iteratif pilote sa profondeur de recherche.",
        "Seule difference entre les deux bras : la consigne.",
        "Metrique principale : les esquives, pas le bon tome.",
        "Verdict de reference : verdict() a trois etats, importe de",
        "exporter_echanges. Le verdict naif est compte en parallele.",
        "",
        "Rappel du code : _format_rag_prompt (llm.py l.196) impose deja",
        "en dur 'Si l'information n'est pas presente, dis-le clairement'.",
        "La consigne s'y oppose sans pouvoir la retirer.",
        "",
    ]

    resultats = {}
    with open(SORTIE, "w", encoding="utf-8") as sortie:
        for bras in ("sans", "avec"):
            lignes += [f"BRAS {bras.upper()}", "-" * 78]
            print(f"\n=== BRAS {bras.upper()} ===")
            compte = {"esquive": 0, "esquive_naif": 0, "bon_tome": 0,
                      "bon_tome_tronque": 0, "mesurables": 0}
            t_bras = time.time()
            for c in cas:
                charge = {
                    "query": c.get("remarque_mrae") or "",
                    "top_k": TOP_K,
                    "deep_search": False,
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
                    "reponse": reponse,
                }, ensure_ascii=False) + "\n")
                sortie.flush()
                marque = (f"  {c['id']:9} "
                          f"{verdict_v:<8} "
                          f"{'naif:ESQ' if esq_naif else '        '} "
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
        f"{'duree (min)':14}{a['duree_min']:>10}{b['duree_min']:>10}",
        "",
        "L'ecart entre les deux lignes bon_tome mesure ce que la",
        "troncature de _extract_sources faisait perdre a la mesure.",
        "",
        "Lecture : une baisse nette des esquives valide la piste de la",
        "consigne. Une stabilite designe la phrase ecrite en dur dans",
        "_format_rag_prompt, et le correctif passe alors par le code.",
        "",
        "Rappel : un cas dont le fragment couvrant est classe au-dela du",
        "centieme rang est attendu en echec quelle que soit la consigne,",
        "cette route tronquant la recherche a 100 passages. Un tel echec",
        "n'est pas imputable a la generation mais a la recherche.",
    ]

    texte = "\n".join(lignes)
    print("\n" + "\n".join(lignes[-16:]))
    with open(RAPPORT, "w", encoding="utf-8") as f:
        f.write(texte + "\n")
    print(f"\nEcrit : {SORTIE}\nEcrit : {RAPPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
