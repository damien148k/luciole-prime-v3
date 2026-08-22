#!/usr/bin/env python3
"""Deux passages identiques du bras consigne seule. Plancher de bruit.

POURQUOI CE SCRIPT EXISTE

Le 3 aout, deux campagnes lancees a temperature 0 et germe 42, sans
aucune modification entre les deux, n'avaient donne que 7 reponses
identiques sur 20 sur la route agentique. Depuis, trois correctifs sont
en service sur l'instance mrae, verifies fichier par fichier le 6 aout a
11 h 45 : germe transmis dans la charge utile, temperature 0.0 et seed 42
dans settings.yaml, reecriveur hors de la boucle.

Tant que le plancher de bruit n'est pas connu, aucun ecart mesure entre
deux bras n'est interpretable. Un ecart de deux cas sur vingt ne veut
rien dire si deux passages du meme bras en produisent trois.

CE QUE FAIT CE SCRIPT

Deux passages du seul bras "question brute + consigne", l'un apres
l'autre, meme ordre, meme charge utile, rien entre les deux. Puis, pour
chacune des vingt remarques :

  - l'empreinte du texte, donc l'identite au caractere pres,
  - la position du premier caractere qui differe, s'il y en a un,
  - l'ecart de longueur,
  - le verdict des trois etats, et surtout s'il change d'un passage a
    l'autre.

CE QUI COMPTE DANS LE RAPPORT

Pas le nombre de reponses identiques, qui serait un ideal inutile, mais
le nombre de VERDICTS qui changent. Si les textes diffusent un peu tout
en gardant le meme verdict, l'instrument est utilisable. Si les verdicts
basculent, aucune comparaison entre bras n'a de sens et le chantier
suivant est le cache d'invites du serveur, pas Luciole.

CE QUE CE SCRIPT NE PEUT PAS FAIRE, lu dans le code avant de l'ecrire

  - la consigne est importee de campagne_consigne.py, elle n'est pas
    recopiee : un seul texte de reference, aucune divergence possible,
  - la route est /api/query2, dont le pipeline pilote sa recherche,
  - le journal Ollama du 6 aout donne n_ctx_slot = 8192 et une invite
    mesuree a 6051 jetons, truncated = 0. La fenetre n'est pas depassee
    sur ce bras, mais la marge est de l'ordre de deux mille jetons,
  - ce meme journal montre le serveur reutilisant partiellement le cache
    d'invites d'une requete a l'autre. C'est la cause suspectee des
    divergences, elle n'est pas neutralisee ici : ce script la mesure,
    il ne la corrige pas.

USAGE

  docker exec luciole-agent-mrae python /app/evaluation/campagne_reproductibilite.py

  PASSAGES=3  pour trois passages au lieu de deux (14 min chacun).
  LABEL=repro prefixe des fichiers de sortie.
"""
import hashlib
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
LABEL = os.environ.get("LABEL", "repro")
TOP_K = int(os.environ.get("TOP_K", "30"))
PASSAGES = int(os.environ.get("PASSAGES", "2"))

SORTIE = os.path.join(BASE, f"campagne_{LABEL}.jsonl")
RAPPORT = os.path.join(BASE, f"rapport_{LABEL}.txt")

sys.path.insert(0, BASE)
from diag_rappel_pages import references_attendues, tome_du_nom  # noqa: E402
from campagne_consigne import CONSIGNE  # noqa: E402

# Detecteur a trois verdicts. Meme regle que campagne_reformulation.py :
# l'absence annoncee d'entree de jeu, ou une reponse trop courte, ou
# l'absence de toute source citee, valent echec. L'absence posee apres la
# substance est une concession, que la consigne prescrit explicitement.
ESQUIVE = re.compile(
    r"ne contiennent (aucune|pas)|aucune information|n'est pas (explicitement )?"
    r"mentionn|ne mentionnent pas|ne fournissent pas|pas d'information|"
    r"ne permettent pas|ne citent pas|n'en parlent pas", re.I)
SOURCE_CITEE = re.compile(r"\.pdf|tome_\d", re.I)


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


def premier_ecart(a, b):
    """Index du premier caractere different, None si les textes coincident."""
    if a == b:
        return None
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


def un_passage(numero, cas, sortie):
    """Les vingt remarques, avec consigne. Retourne un dict id -> resultat."""
    print(f"\n=== PASSAGE {numero} ===")
    res_passage = {}
    for c in cas:
        charge = {
            "query": c.get("remarque_mrae") or "",
            "top_k": TOP_K,
            "deep_search": False,
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
        tomes = tomes_des_sources(sources)
        attendus = {r[0] for r in
                    references_attendues(c.get("sources_citees_wpd"))}
        v = verdict(reponse)
        ligne = {
            "id": c["id"],
            "passage": numero,
            "erreur": erreur,
            "verdict": v,
            "bon_tome": bool(tomes & attendus) if attendus else None,
            "duree_s": round(duree, 1),
            "longueur_reponse": len(reponse),
            "empreinte": hashlib.sha1(reponse.encode("utf-8")).hexdigest()[:12],
            "tomes_rendus": sorted(tomes),
            "nb_sources": len(sources),
            "reponse": reponse,
        }
        sortie.write(json.dumps(ligne, ensure_ascii=False) + "\n")
        sortie.flush()
        res_passage[c["id"]] = ligne
        marque = (f"  {c['id']:9} {v:8} {ligne['empreinte']} "
                  f"{duree:5.1f}s  {len(reponse):5} car.")
        if erreur:
            marque += f"  ERREUR {erreur[:60]}"
        print(marque)
    return res_passage


def main():
    cas = [json.loads(x) for x in open(JEU, encoding="utf-8") if x.strip()]
    lignes = [
        f"CAMPAGNE {LABEL} : plancher de bruit du bras consigne seule",
        "=" * 78,
        f"{PASSAGES} passages identiques, {len(cas)} remarques chacun.",
        f"route /api/query2   top_k={TOP_K}",
        "consigne importee de campagne_consigne.py, "
        f"{len(CONSIGNE)} caracteres.",
        "",
        "Etat de l'instance au moment de la mesure, verifie fichier par",
        "fichier : code de main 7dbdbac, germe transmis, temperature 0.0",
        "et seed 42 dans settings.yaml.",
        "",
        "Repere du 3 aout, avant ces correctifs, sur la route agentique :",
        "7 reponses identiques sur 20 entre deux passages.",
        "",
    ]

    passages = []
    t_total = time.time()
    with open(SORTIE, "w", encoding="utf-8") as sortie:
        for n in range(1, PASSAGES + 1):
            lignes += [f"PASSAGE {n}", "-" * 78]
            t0 = time.time()
            p = un_passage(n, cas, sortie)
            passages.append(p)
            duree = (time.time() - t0) / 60
            lignes += [f"  {ligne['id']:9} {ligne['verdict']:8} "
                       f"{ligne['empreinte']} {ligne['duree_s']:5.1f}s  "
                       f"{ligne['longueur_reponse']:5} car."
                       for ligne in p.values()]
            lignes += [f"  duree du passage : {duree:.1f} min", ""]

    # ── Comparaison, passage 1 contre chacun des suivants ────────────────
    lignes += ["COMPARAISON", "-" * 78,
               f"{'id':10}{'identique':>11}{'1er ecart':>11}"
               f"{'car. p1':>9}{'ecart len':>11}  verdicts"]
    ref = passages[0]
    identiques = 0
    verdicts_changes = []
    ecarts = []
    for cid in [c["id"] for c in cas]:
        a = ref[cid]
        autres = [p[cid] for p in passages[1:]]
        tous_identiques = all(x["empreinte"] == a["empreinte"] for x in autres)
        if tous_identiques:
            identiques += 1
        pos = [premier_ecart(a["reponse"], x["reponse"]) for x in autres]
        pos = [p for p in pos if p is not None]
        if pos:
            ecarts.append(min(pos))
        vs = [a["verdict"]] + [x["verdict"] for x in autres]
        change = len(set(vs)) > 1
        if change:
            verdicts_changes.append((cid, vs))
        lignes.append(
            f"{cid:10}{('oui' if tous_identiques else 'non'):>11}"
            f"{(str(min(pos)) if pos else '-'):>11}"
            f"{a['longueur_reponse']:>9}"
            f"{(','.join(str(x['longueur_reponse'] - a['longueur_reponse']) for x in autres)):>11}"
            f"  {' '.join(vs)}{'   <-- CHANGE' if change else ''}")

    bons = []
    for n, p in enumerate(passages, 1):
        mesurables = [x for x in p.values() if x["bon_tome"] is not None]
        bons.append("%d/%d" % (sum(1 for x in mesurables if x["bon_tome"]),
                               len(mesurables)))
    compte_verdicts = []
    for n, p in enumerate(passages, 1):
        c = {}
        for x in p.values():
            c[x["verdict"]] = c.get(x["verdict"], 0) + 1
        compte_verdicts.append("p%d: %s" % (n, " ".join(
            "%s=%d" % (k, c.get(k, 0))
            for k in ("repond", "concede", "ESQUIVE"))))

    lignes += [
        "",
        "SYNTHESE", "-" * 78,
        f"  reponses identiques au caractere : {identiques}/{len(cas)}",
        f"  verdicts qui changent            : {len(verdicts_changes)}/{len(cas)}",
        f"  premier ecart, position mediane  : "
        f"{sorted(ecarts)[len(ecarts) // 2] if ecarts else '-'} caracteres",
        f"  bon_tome par passage             : {', '.join(bons)}",
        "  verdicts par passage             : " + " | ".join(compte_verdicts),
        f"  duree totale                     : "
        f"{(time.time() - t_total) / 60:.1f} min",
        "",
    ]
    if verdicts_changes:
        lignes.append("  verdicts instables :")
        for cid, vs in verdicts_changes:
            lignes.append(f"      {cid}  {' -> '.join(vs)}")
        lignes.append("")
    lignes += [
        "LECTURE",
        "-" * 78,
        "  Zero verdict instable : l'instrument est utilisable, un ecart",
        "  d'un cas entre deux bras devient un fait et non du bruit.",
        "",
        "  Un ou deux verdicts instables : tout ecart inferieur ou egal a",
        "  ce nombre, entre deux bras, doit etre tenu pour indistinct.",
        "",
        "  Trois ou plus : aucune des comparaisons entre bras faites",
        "  jusqu'ici ne tient, et le chantier suivant est le cache",
        "  d'invites du serveur Ollama, pas Luciole.",
    ]

    texte = "\n".join(lignes)
    print("\n" + "\n".join(lignes[lignes.index("SYNTHESE") - 1:]))
    with open(RAPPORT, "w", encoding="utf-8") as f:
        f.write(texte + "\n")
    print(f"\nEcrit : {SORTIE}\nEcrit : {RAPPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
