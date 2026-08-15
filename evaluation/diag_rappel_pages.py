#!/usr/bin/env python3
"""Plafond de la recherche seule, au niveau de la page. Sans LLM.

diag_recherche.py mesure le rappel au niveau du document (le bon tome
remonte-t-il). Celui-ci descend d'un cran : la bonne PAGE remonte-t-elle,
puisque c'est ce que l'agent doit citer et c'est la que la mesure de la
campagne s'effondre (bon tome present dans les passages 8/10, bonne page
2/10).

Trois questions, et rien d'autre :

  1. Jusqu'ou monte le rappel de page quand on augmente top_k ? Si la
     bonne page n'est jamais dans les cent premiers passages, aucun
     reglage de prompt ni d'agent ne la fera citer.
  2. La formulation de la requete change-t-elle quelque chose ? Deux
     variantes deterministes, sans modele : la remarque brute, et la
     remarque privee de son amorce "l'autorite environnementale
     recommande de".
  3. Y a-t-il un decalage systematique entre la page citee par wpd, qui
     est la page imprimee, et page_start du fragment, qui est la page du
     conteneur PDF ? Un decalage constant se verrait immediatement dans
     la distribution des ecarts signes, et invaliderait alors la mesure
     de rappel de page telle quelle.

Aucun appel au modele de generation : le resultat est deterministe et
tient en quelques dizaines de secondes.
"""
import json
import os
import re
import statistics
import sys

sys.path.insert(0, "/app")

BASE = os.environ.get("EVAL_DIR", "/app/evaluation")
JEU = os.path.join(BASE, "jeu_test_mrae.jsonl")
SORTIE = os.path.join(BASE, "rapport_rappel_pages.txt")
DETAIL = os.path.join(BASE, "detail_rappel_pages.jsonl")

TOP_K = [int(x) for x in os.environ.get("TOP_K", "10,30,50,100").split(",")]
TOLERANCES = [0, 2, 5, 10]

# 'Tome 1 p. 27', 'Tome 5 p. 202-203', et la forme sans page 'Tome 3'.
MOTIF_REF = re.compile(
    r"tome[\s_\-]*(\d+)(?:\D{0,12}?p\.?\s*(\d+)(?:\s*[-\u2013]\s*(\d+))?)?",
    re.I)
AMORCE = re.compile(
    r"^.{0,80}?l[’'\s]autorit[ée]\s+environnementale\s+recommande\s*"
    r"(?:de\s+|d[’']|que\s+|au\s+|aux\s+|:\s*)?",
    re.I | re.S)


def deriver(texte):
    """Retire l'amorce administrative, qui est identique dans les vingt
    remarques et ne porte donc aucune information discriminante."""
    t = " ".join(str(texte or "").split())
    nouveau = AMORCE.sub("", t, count=1).strip()
    return nouveau if len(nouveau) > 20 else t


def references_attendues(chaines):
    """[(tome, page_debut, page_fin)] extraites des citations de wpd.

    Une citation en plage ('p. 202-203') donne l'intervalle complet : un
    fragment qui couvre la page 203 repond bien a cette citation. Les
    doublons sont ecartes, mrae-07 citant deux fois 'Tome 5 p. 202', ce
    qui gonflerait artificiellement le denominateur.
    """
    refs = []
    for c in chaines or []:
        c = str(c) or ""
        trouve = False
        for m in MOTIF_REF.finditer(c):
            trouve = True
            if not m.group(2):
                ref = (int(m.group(1)), None, None)
            else:
                debut = int(m.group(2))
                fin = int(m.group(3)) if m.group(3) else debut
                ref = (int(m.group(1)), debut, fin)
            if ref not in refs:
                refs.append(ref)
        # Repli : citation = nom de fichier a numero en tete
        # ('5 - PE de la Paniere du Fort - ...pdf'), sans motif 'tome'.
        if not trouve:
            t = tome_du_nom(c)
            if t is not None and (t, None, None) not in refs:
                refs.append((t, None, None))
    return fusionner(refs)


def fusionner(refs):
    """Fusionne les intervalles d'un meme tome qui se recouvrent.

    'Tome 5 p. 202-203' et 'Tome 5 p. 202' designent la meme cible : les
    compter deux fois gonflerait le denominateur et ferait passer mrae-07,
    qui cite cinq extraits contigus, pour cinq exigences distinctes.
    """
    sans_page = [r for r in refs if r[1] is None]
    avec_page = sorted((r for r in refs if r[1] is not None),
                       key=lambda r: (r[0], r[1], r[2]))
    fusionnees = []
    for tome, debut, fin in avec_page:
        if fusionnees:
            t, d, f = fusionnees[-1]
            if t == tome and debut <= f + 1:
                fusionnees[-1] = (t, d, max(f, fin))
                continue
        fusionnees.append((tome, debut, fin))
    return fusionnees + sans_page


def tome_du_nom(nom):
    """Numero de tome d'un nom de fichier.

    Deux conventions vues en corpus : '..._tome_5_...' (Saint-Maixent)
    et, depuis la Paniere du Fort, un numero en tete de nom
    ('5 - PE de la Paniere du Fort - ...pdf', '5-1 - ...pdf'). Le repli
    accepte les deux, le motif 'tome' restant prioritaire.
    """
    nom = str(nom) or ""
    m = re.search(r"tome[\s_\-]*(\d+)", nom, re.I)
    if m:
        return int(m.group(1))
    m = re.match(r"^\s*(\d+)(?:[\s\-]|$)", nom)
    return int(m.group(1)) if m else None


def decrire_passage(res, rang):
    """(rang, tome, page_debut, page_fin) d'un resultat du moteur."""
    meta = (res.get("metadata") if isinstance(res, dict) else None) or {}
    nom = meta.get("file_name") or (res.get("file_name")
                                    if isinstance(res, dict) else None)
    def entier(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    debut = entier(meta.get("page_start"))
    fin = entier(meta.get("page_end"))
    if fin is None:
        fin = debut
    return {"rang": rang, "tome": tome_du_nom(nom), "nom": nom,
            "page_debut": debut, "page_fin": fin}


def ecart(attendu_debut, attendu_fin, p):
    """Distance en pages entre l'intervalle attendu et celui du fragment.

    Nulle si les deux intervalles se recouvrent. Negative si le fragment
    est avant les pages attendues, positive s'il est apres. Le signe est
    ce qui permet de detecter un decalage systematique de pagination,
    une valeur absolue le masquerait.
    """
    debut, fin = p["page_debut"], p["page_fin"]
    if debut is None:
        return None
    if fin is None:
        fin = debut
    if debut <= attendu_fin and attendu_debut <= fin:
        return 0
    if fin < attendu_debut:
        return fin - attendu_debut
    return debut - attendu_fin


def main():
    from src.agent.api import get_analyzer
    moteur = get_analyzer().hybrid_search

    cas = [json.loads(x) for x in open(JEU, encoding="utf-8") if x.strip()]
    retenus = []
    for c in cas:
        refs = [r for r in references_attendues(c.get("sources_citees_wpd"))
                if r[1] is not None]
        if refs:
            retenus.append((c, refs))

    lignes = [
        "PLAFOND DE LA RECHERCHE SEULE, AU NIVEAU DE LA PAGE",
        "=" * 78,
        f"Cas du jeu de test : {len(cas)}",
        f"Cas avec au moins une reference (tome + page) attendue : {len(retenus)}",
        f"top_k mesures : {TOP_K}",
        "Aucun appel au modele de generation.",
        "",
    ]

    if not retenus:
        lignes.append("Aucune reference exploitable, mesure impossible.")
        texte = "\n".join(lignes)
        print(texte)
        return 1

    # Structure d'un resultat, pour ne pas deviner les clefs de metadonnees.
    apercu = moteur.search("raccordement electrique poste source", top_k=2)
    lignes += ["STRUCTURE D'UN RESULTAT", "-" * 78]
    if apercu:
        r0 = apercu[0]
        lignes.append(f"  clefs   = {sorted(r0.keys()) if isinstance(r0, dict) else type(r0)}")
        lignes.append(f"  metadata= {str((r0 or {}).get('metadata'))[:300]}")
    else:
        lignes.append("  aucun resultat, le moteur repond vide")
    lignes.append("")

    cumul = {}
    detail = []
    ecarts_signes = []

    for c, refs in retenus:
        brute = " ".join(str(c["remarque_mrae"]).split())
        variantes = (("brute", brute), ("derivee", deriver(brute)))
        lignes.append(f"{c['id']}  attendu: " + ", ".join(
            f"tome {t} p.{d}" + (f"-{f}" if f != d else "")
            for t, d, f in refs))

        for nom_var, requete in variantes:
            for k in TOP_K:
                try:
                    res = moteur.search(requete, top_k=k)
                except Exception as e:
                    lignes.append(f"   {nom_var:8} k={k:<4} ERREUR "
                                  f"{type(e).__name__}: {e}")
                    continue
                passages = [decrire_passage(r, i)
                            for i, r in enumerate(res, 1)]
                sans_page = sum(1 for p in passages
                                if p["page_debut"] is None)

                for tome, att_debut, att_fin in refs:
                    memes = [p for p in passages if p["tome"] == tome]
                    ecarts = [(ecart(att_debut, att_fin, p), p) for p in memes]
                    ecarts = [(e, p) for e, p in ecarts if e is not None]
                    meilleur = min(ecarts, key=lambda x: abs(x[0])) if ecarts else None

                    cle = (nom_var, k)
                    c_ = cumul.setdefault(cle, {"n": 0, "tome": 0,
                                                "sans_page": 0,
                                                **{f"tol{t}": 0 for t in TOLERANCES}})
                    c_["n"] += 1
                    c_["sans_page"] += sans_page
                    if memes:
                        c_["tome"] += 1
                    if meilleur is not None:
                        for t in TOLERANCES:
                            if abs(meilleur[0]) <= t:
                                c_[f"tol{t}"] += 1
                        if k == max(TOP_K) and nom_var == "derivee":
                            ecarts_signes.append((c["id"], tome, att_debut,
                                                  meilleur[0]))

                    rang = meilleur[1]["rang"] if meilleur and meilleur[0] == 0 else "-"
                    lignes.append(
                        f"   {nom_var:8} k={k:<4} tome {tome} p.{att_debut:<4} "
                        f"passages du tome={len(memes):<3} "
                        f"ecart min={meilleur[0] if meilleur else 'n/a':<6} "
                        f"rang page exacte={rang}")
                    detail.append({
                        "id": c["id"], "variante": nom_var, "top_k": k,
                        "tome": tome, "page_attendue": att_debut,
                        "page_attendue_fin": att_fin,
                        "passages_du_tome": len(memes),
                        "ecart_min": meilleur[0] if meilleur else None,
                        "rang_page_exacte": rang if rang != "-" else None,
                        "passages_sans_page": sans_page,
                    })
        lignes.append("")

    lignes += ["SYNTHESE : rappel par variante et par top_k", "-" * 78,
               f"{'variante':10} {'k':>5} {'n':>4} {'tome':>7} " +
               " ".join(f"{'p+-' + str(t):>8}" for t in TOLERANCES)]
    for (nom_var, k), v in sorted(cumul.items(), key=lambda x: (x[0][0], x[0][1])):
        n = v["n"]
        lignes.append(
            f"{nom_var:10} {k:5} {n:4} {v['tome']:>3}/{n:<3} " +
            " ".join(f"{v['tol' + str(t)]:>4}/{n:<3}" for t in TOLERANCES))

    lignes += ["", "DECALAGE DE PAGINATION", "-" * 78,
               "Ecart signe entre la page citee par wpd et le fragment le plus",
               "proche du bon tome (variante derivee, k max). Negatif : le",
               "fragment est avant la page citee.", ""]
    if ecarts_signes:
        for ident, tome, page_att, e in ecarts_signes:
            lignes.append(f"  {ident:9} tome {tome} p.{page_att:<4} ecart {e:+d}")
        valeurs = [e for _, _, _, e in ecarts_signes]
        lignes.append("")
        lignes.append(f"  mediane {statistics.median(valeurs):+.1f}  "
                      f"moyenne {statistics.mean(valeurs):+.1f}  "
                      f"min {min(valeurs):+d}  max {max(valeurs):+d}")
        lignes.append("  Un decalage systematique se lirait comme une mediane")
        lignes.append("  franchement non nulle avec une faible dispersion.")
    else:
        lignes.append("  aucun ecart calculable")

    total_sans_page = sum(v["sans_page"] for v in cumul.values())
    lignes += ["", "CONTROLE : passages sans pagination", "-" * 78,
               f"  occurrences cumulees de fragments sans page_start : "
               f"{total_sans_page}",
               "  Une valeur non nulle rendrait la mesure de page partielle."]

    texte = "\n".join(lignes)
    print(texte)
    with open(SORTIE, "w", encoding="utf-8") as f:
        f.write(texte + "\n")
    with open(DETAIL, "w", encoding="utf-8") as f:
        for d in detail:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print(f"\nEcrit : {SORTIE}")
    print(f"Ecrit : {DETAIL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
