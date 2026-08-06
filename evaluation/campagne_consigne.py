#!/usr/bin/env python3
"""La consigne de generation corrige-t-elle l'ecart de registre ?

Defaut mesure sur la campagne sans_agent : dans 18 reponses sur 20,
Luciole lit la remarque comme une question documentaire, cherche la
recommandation de l'autorite environnementale dans l'etude d'impact, ne
l'y trouve pas, et repond qu'elle n'y est pas. Exemple mrae-15 : "les
extraits fournis ne mentionnent pas explicitement la recommandation de
l'autorite environnementale concernant l'utilisation de la technologie
radar". C'est exact et hors sujet : la recommandation porte sur le
dossier, elle n'est pas dedans.

Ce script fait tourner les vingt remarques deux fois, sans consigne puis
avec, tout le reste identique. Un seul passage suffit par bras : le
plancher de bruit a ete mesure nul sur ce systeme.

METRIQUE PRINCIPALE : le nombre d'esquives, pas le bon tome. La grille
bon_tome ne discrimine plus rien sur ce corpus, trois documents
apparaissant dans 17 a 19 reponses sur 20.

CE QUE CE TEST NE PEUT PAS FAIRE, lu dans le code avant de l'ecrire :

  - OptionsModel (src/agent/api.py l.70) n'a que detail_level, max_items
    et include_sources. /api/analyze ne peut donc PAS porter de
    custom_prompt. La route utilisee ici est /api/query, seule a exposer
    ce champ (l.103),
  - mais /api/query ne passe pas detail_level, donc le mode standard
    s'applique : max_total_chunks=100 au lieu de 500. Le fragment
    couvrant de mrae-13, au rang 183, restera hors d'atteinte. Ce cas est
    attendu en echec et n'est pas imputable a la consigne,
  - llm.py l.190, _build_system_prompt AJOUTE la consigne au prompt de
    base, il ne le remplace pas,
  - llm.py l.196, _format_rag_prompt termine le message utilisateur par
    "Reponds en t'appuyant exclusivement sur les extraits ci-dessus. Si
    l'information n'est pas presente, dis-le clairement." Cette phrase
    est ecrite en dur et produit exactement le comportement qu'on essaie
    de corriger. La consigne entre donc en concurrence avec elle.

Si le bras avec consigne ne bouge pas, la conclusion n'est pas que la
consigne est mauvaise : c'est que le correctif doit se faire dans
_format_rag_prompt, donc en code.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("EVAL_DIR", "/app/evaluation")
# Jeu de test fourni par l'instance, jamais versionne : il porte les
# remarques d'un dossier reel. Surchargeable pour un autre dossier,
# par exemple JEU=/app/evaluation/jeu_test_beaumont_sud.jsonl
JEU = os.environ.get("JEU", os.path.join(BASE, "jeu_test_mrae.jsonl"))
API = os.environ.get("LUCIOLE_API", "http://localhost:8000")
LABEL = os.environ.get("LABEL", "consigne")
TOP_K = int(os.environ.get("TOP_K", "30"))

SORTIE = os.path.join(BASE, f"campagne_{LABEL}.jsonl")
RAPPORT = os.path.join(BASE, f"rapport_{LABEL}.txt")

sys.path.insert(0, BASE)
from diag_rappel_pages import references_attendues, tome_du_nom  # noqa: E402

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
        API + "/api/query", data=donnees,
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
        f"route /api/query   top_k={TOP_K}   enable_rewriting=false",
        "Seule difference entre les deux bras : la consigne.",
        "Metrique principale : les esquives, pas le bon tome.",
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
            compte = {"esquive": 0, "bon_tome": 0, "mesurables": 0}
            t_bras = time.time()
            for c in cas:
                charge = {
                    "query": c.get("remarque_mrae") or "",
                    "top_k": TOP_K,
                    "enable_rewriting": False,
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
                tomes = tomes_des_sources(sources)
                attendus = {r[0] for r in
                            references_attendues(c.get("sources_citees_wpd"))}
                esq = bool(ESQUIVE.search(reponse))
                bon = bool(attendus and (tomes & attendus))
                if attendus:
                    compte["mesurables"] += 1
                    compte["bon_tome"] += int(bon)
                compte["esquive"] += int(esq)

                sortie.write(json.dumps({
                    "id": c["id"], "bras": bras, "erreur": erreur,
                    "esquive": esq, "bon_tome": bon if attendus else None,
                    "duree_s": round(duree, 1),
                    "longueur_reponse": len(reponse),
                    "tomes_rendus": sorted(tomes),
                    "nb_sources": len(sources),
                    "reponse": reponse,
                }, ensure_ascii=False) + "\n")
                sortie.flush()
                marque = (f"  {c['id']:9} "
                          f"{'ESQUIVE' if esq else 'repond ':<8} "
                          f"{'bon_tome' if bon else '        '} "
                          f"{duree:5.1f}s  {len(reponse):5} car.")
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
        f"{'esquives':14}{a['esquive']:>10}{b['esquive']:>10}   sur 20",
        f"{'bon_tome':14}{a['bon_tome']:>10}{b['bon_tome']:>10}   "
        f"sur {a['mesurables']} mesurables",
        f"{'duree (min)':14}{a['duree_min']:>10}{b['duree_min']:>10}",
        "",
        "Lecture : une baisse nette des esquives valide la piste de la",
        "consigne. Une stabilite designe la phrase ecrite en dur dans",
        "_format_rag_prompt, et le correctif passe alors par le code.",
        "",
        "Rappel : mrae-13 est attendu en echec quelle que soit la",
        "consigne, son fragment couvrant etant au rang 183 alors que",
        "cette route tronque la recherche a 100 passages.",
    ]

    texte = "\n".join(lignes)
    print("\n" + "\n".join(lignes[-16:]))
    with open(RAPPORT, "w", encoding="utf-8") as f:
        f.write(texte + "\n")
    print(f"\nEcrit : {SORTIE}\nEcrit : {RAPPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
