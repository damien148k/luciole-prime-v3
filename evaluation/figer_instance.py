#!/usr/bin/env python3
"""Releve complet d'une instance Luciole, en vue de la refaire ailleurs.

POURQUOI

Le depot ne porte que le code. Lu dans son .gitignore au commit 9b411a5 :
.env, config/settings.yaml, config/auth.yaml, data/inbox, data/processed,
data/uploads, opensearch_data, models, feedbacks, docker-compose.override.yml
sont exclus. Un clone ne rejoue donc pas les resultats mesures les 5 et 6
aout 2026 : les reglages qui les produisent ne sont pas dedans.

Ce script releve, depuis le conteneur agent, tout ce qui n'est pas dans le
depot et qui influence une reponse. Il ne suppose rien : chaque valeur est
lue dans le fichier ou obtenue du service qui la detient.

CE QU'IL ECRIT

  /app/evaluation/paquet_<instance>/
      settings.yaml            configuration effective, secrets masques
      prompts.yaml             si present
      synonyms.txt             si present
      environnement.txt        variables du conteneur, secrets masques
      modeles.json             modeles Ollama servis, avec empreintes
      index.json              index OpenSearch et collections Qdrant
      corpus.txt               fichiers du corpus, taille et empreinte
      empreintes_code.txt      empreinte git de chaque .py de /app/src
      evaluation.txt           scripts et jeux de test presents
      RELEVE.txt               synthese lisible et points de vigilance
  /app/evaluation/paquet_<instance>.zip

USAGE

  docker exec luciole-agent-mrae python /app/evaluation/figer_instance.py
  docker cp luciole-agent-mrae:/app/evaluation/paquet_mrae.zip C:\\RAG\\luciole-mrae\\campagnes\\

CE QUE LE PAQUET NE CONTIENT PAS, VOLONTAIREMENT

Le corpus lui-meme, les poids de modeles, les index et les bases de retours.
Trop lourds, et reconstructibles ou recopiables directement. Le releve en
donne l'inventaire exact, empreintes comprises, pour verifier apres coup que
la machine cible porte bien les memes documents.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile

CONFIG = os.environ.get("CONFIG_DIR", "/app/config")
SRC = "/app/src"
EVAL = os.environ.get("EVAL_DIR", "/app/evaluation")
DATA = "/app/data"
INSTANCE = os.environ.get("INSTANCE_NAME", "instance")
LLM_URL = os.environ.get("LLM_URL", "http://ollama:11434")
OS_URL = os.environ.get("OPENSEARCH_URL", "http://opensearch:9200")
QD_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")

PAQUET = os.path.join(EVAL, f"paquet_{INSTANCE}")

# Tout ce qui ressemble a un secret est remplace, jamais recopie.
SECRET = re.compile(
    r"(password|passwd|secret|token|api_key|apikey|access_key|"
    r"private_key|smtp_pass|hash|salt)", re.I)

notes = []


def masquer(texte):
    """Remplace la valeur des lignes dont la cle evoque un secret."""
    sorties = []
    for ligne in texte.splitlines():
        if SECRET.search(ligne) and ":" in ligne:
            cle, _, reste = ligne.partition(":")
            if reste.strip() and not reste.strip().startswith("#"):
                sorties.append(f"{cle}: <<masque par figer_instance>>")
                continue
        sorties.append(ligne)
    return "\n".join(sorties) + "\n"


def http_json(url, defaut=None, methode="GET", charge=None):
    """Appel HTTP sans dependance : urllib de la bibliotheque standard."""
    import urllib.error
    import urllib.request
    try:
        donnees = json.dumps(charge).encode() if charge is not None else None
        req = urllib.request.Request(url, data=donnees, method=methode)
        if donnees:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=20) as r:
            brut = r.read().decode("utf-8", "replace")
        try:
            return json.loads(brut)
        except ValueError:
            return {"brut": brut}
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        notes.append(f"service injoignable, {url} : {e}")
        return defaut


def empreinte_git(chemin):
    """Meme empreinte que git, donc comparable a n'importe quel commit."""
    with open(chemin, "rb") as f:
        contenu = f.read()
    entete = f"blob {len(contenu)}\0".encode()
    return hashlib.sha1(entete + contenu).hexdigest()


def empreinte_fichier(chemin):
    h = hashlib.sha256()
    with open(chemin, "rb") as f:
        for bloc in iter(lambda: f.read(1 << 20), b""):
            h.update(bloc)
    return h.hexdigest()


def copier_config():
    """Configuration et secrets masques."""
    releve = {}
    for nom in ("settings.yaml", "prompts.yaml", "synonyms.txt"):
        source = os.path.join(CONFIG, nom)
        if not os.path.exists(source):
            notes.append(f"absent de {CONFIG} : {nom}")
            continue
        texte = open(source, encoding="utf-8", errors="replace").read()
        cible = os.path.join(PAQUET, nom)
        with open(cible, "w", encoding="utf-8") as f:
            f.write(masquer(texte) if nom.endswith(".yaml") else texte)
        releve[nom] = len(texte.splitlines())

    if os.path.exists(os.path.join(CONFIG, "auth.yaml")):
        notes.append("config/auth.yaml existe et n'est PAS recopie : "
                     "il porte les comptes. A transporter a la main.")

    return releve


def reglages_critiques():
    """Les valeurs qui decident d'une reponse, lues dans settings.yaml."""
    p = os.path.join(CONFIG, "settings.yaml")
    if not os.path.exists(p):
        return {}
    texte = open(p, encoding="utf-8", errors="replace").read()
    cles = ("temperature", "seed", "max_tokens", "num_ctx", "fusion_top_k",
            "rerank_top_n", "bm25_top_k", "dense_top_k", "chunk_size",
            "chunk_overlap", "model", "provider", "engine")
    trouves = []
    for ligne in texte.splitlines():
        nu = ligne.strip()
        if nu.startswith("#") or ":" not in nu:
            continue
        cle = nu.split(":")[0].strip()
        if cle in cles:
            trouves.append(nu)
    return trouves


def relever_modeles():
    """Modeles servis, avec les parametres reellement appliques."""
    tags = http_json(f"{LLM_URL}/api/tags", {})
    sortie = {"llm_url": LLM_URL, "modeles": []}
    for m in (tags or {}).get("models", []):
        nom = m.get("name")
        detail = http_json(f"{LLM_URL}/api/show", {}, "POST", {"model": nom})
        params = (detail or {}).get("parameters", "")
        sortie["modeles"].append({
            "nom": nom,
            "empreinte": (m.get("digest") or "")[:16],
            "octets": m.get("size"),
            "modifie": m.get("modified_at"),
            "parametres_modelfile": params,
            "famille": (m.get("details") or {}).get("family"),
            "quantification": (m.get("details") or {}).get(
                "quantization_level"),
        })
        if params and "num_ctx" in params:
            for ligne in params.splitlines():
                if "num_ctx" in ligne:
                    notes.append(f"modele {nom} : {ligne.strip()} "
                                 "(fenetre reellement servie)")
    if not sortie["modeles"]:
        notes.append("aucun modele releve : Ollama repond-il ?")
    return sortie


def relever_index():
    sortie = {}
    cat = http_json(f"{OS_URL}/_cat/indices?format=json", [])
    sortie["opensearch"] = [
        {"index": i.get("index"), "documents": i.get("docs.count"),
         "taille": i.get("store.size"), "etat": i.get("health")}
        for i in (cat or []) if not str(i.get("index", "")).startswith(".")
    ]
    cols = http_json(f"{QD_URL}/collections", {})
    liste = []
    for c in ((cols or {}).get("result") or {}).get("collections", []):
        nom = c.get("name")
        detail = http_json(f"{QD_URL}/collections/{nom}", {})
        res = (detail or {}).get("result", {})
        liste.append({
            "collection": nom,
            "points": res.get("points_count"),
            "vecteurs": res.get("vectors_count"),
            "etat": res.get("status"),
        })
    sortie["qdrant"] = liste
    return sortie


def relever_corpus():
    """Inventaire du corpus : de quoi verifier l'identite ailleurs."""
    lignes = []
    total = 0
    for sous in ("inbox", "processed", "uploads"):
        base = os.path.join(DATA, sous)
        if not os.path.isdir(base):
            continue
        for racine, _, fichiers in os.walk(base):
            for nom in sorted(fichiers):
                if nom.startswith("."):
                    continue
                chemin = os.path.join(racine, nom)
                try:
                    taille = os.path.getsize(chemin)
                    emp = empreinte_fichier(chemin)[:16]
                except OSError as e:
                    lignes.append(f"  illisible  {chemin}  {e}")
                    continue
                total += 1
                rel = os.path.relpath(chemin, DATA)
                lignes.append(f"  {emp}  {taille:>12}  {rel}")
    if not total:
        notes.append("aucun document trouve sous /app/data : le corpus "
                     "n'est pas la ou ce script le cherche.")
    return lignes, total


def relever_code():
    """Empreinte git de chaque .py, pour comparer a un commit du depot."""
    lignes = []
    for racine, dossiers, fichiers in os.walk(SRC):
        dossiers[:] = [d for d in dossiers if d != "__pycache__"]
        for nom in sorted(fichiers):
            if not nom.endswith(".py"):
                continue
            chemin = os.path.join(racine, nom)
            rel = os.path.relpath(chemin, SRC)
            try:
                lignes.append(f"{empreinte_git(chemin)}  src/{rel}")
            except OSError as e:
                lignes.append(f"illisible  src/{rel}  {e}")
    return lignes


def relever_evaluation():
    lignes = []
    for nom in sorted(os.listdir(EVAL)):
        chemin = os.path.join(EVAL, nom)
        if os.path.isdir(chemin) or nom.startswith("paquet_"):
            continue
        try:
            lignes.append(f"  {os.path.getsize(chemin):>10}  {nom}")
        except OSError:
            continue
    return lignes


def relever_environnement():
    lignes = []
    for cle in sorted(os.environ):
        valeur = os.environ[cle]
        if SECRET.search(cle):
            valeur = "<<masque par figer_instance>>"
        lignes.append(f"{cle}={valeur}")
    return lignes


def main():
    if os.path.isdir(PAQUET):
        shutil.rmtree(PAQUET)
    os.makedirs(PAQUET, exist_ok=True)

    print(f"Releve de l'instance {INSTANCE}, profil {PROFIL}")
    fichiers_config = copier_config()
    reglages = reglages_critiques()
    modeles = relever_modeles()
    index = relever_index()
    corpus, nb_corpus = relever_corpus()
    code = relever_code()
    evaluation = relever_evaluation()
    environnement = relever_environnement()

    ecrire = {
        "modeles.json": json.dumps(modeles, ensure_ascii=False, indent=2),
        "index.json": json.dumps(index, ensure_ascii=False, indent=2),
        "corpus.txt": "\n".join(corpus),
        "empreintes_code.txt": "\n".join(code),
        "evaluation.txt": "\n".join(evaluation),
        "environnement.txt": "\n".join(environnement),
    }
    for nom, contenu in ecrire.items():
        with open(os.path.join(PAQUET, nom), "w", encoding="utf-8") as f:
            f.write(contenu + "\n")

    version_docker = ""
    try:
        version_docker = subprocess.run(
            ["python", "--version"], capture_output=True, text=True,
            timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass

    lignes = [
        f"RELEVE D'INSTANCE LUCIOLE : {INSTANCE}",
        "=" * 78,
        f"  profil agent resolu   : {PROFIL}",
        f"  python du conteneur   : {version_docker}",
        f"  fichiers de config    : {', '.join(fichiers_config) or 'aucun'}",
        f"  fichiers .py de src   : {len(code)}",
        f"  documents du corpus   : {nb_corpus}",
        "",
        "REGLAGES QUI DECIDENT D'UNE REPONSE, lus dans config/settings.yaml",
        "-" * 78,
    ]
    lignes += [f"  {r}" for r in reglages] or ["  settings.yaml introuvable"]

    lignes += ["", "MODELES SERVIS", "-" * 78]
    for m in modeles["modeles"]:
        lignes.append(f"  {m['nom']}  empreinte {m['empreinte']}  "
                      f"{m['quantification'] or '?'}")
        for ligne in (m["parametres_modelfile"] or "").splitlines():
            if ligne.strip():
                lignes.append(f"      {ligne.strip()}")

    lignes += ["", "INDEX", "-" * 78]
    for i in index.get("opensearch", []):
        lignes.append(f"  opensearch  {i['index']}  {i['documents']} docs  "
                      f"{i['taille']}  {i['etat']}")
    for c in index.get("qdrant", []):
        lignes.append(f"  qdrant      {c['collection']}  {c['points']} points"
                      f"  {c['etat']}")

    lignes += ["", "SCRIPTS ET JEUX DE TEST PRESENTS", "-" * 78] + evaluation

    lignes += [
        "",
        "A TRANSPORTER A LA MAIN, hors de ce paquet",
        "-" * 78,
        "  data/            le corpus lui-meme, inventorie dans corpus.txt",
        "  models/          les poids, retelechargeables",
        "  feedbacks/       ragas.db et feedbacks.db, l'historique des echanges",
        "  config/auth.yaml les comptes",
        "  .env             ports et nom d'instance, reconstruits a l'install",
        "",
        "POINTS DE VIGILANCE RELEVES",
        "-" * 78,
    ]
    lignes += [f"  {n}" for n in notes] or ["  aucun"]

    lignes += [
        "",
        "COMMENT VERIFIER QUE LA MACHINE CIBLE REND LA MEME CHOSE",
        "-" * 78,
        "  Rejouer campagne_consigne.py sur le meme jeu de questions, sur",
        "  la machine de reference puis sur la cible, et comparer cas par",
        "  cas : verdict, tomes rendus, longueur de reponse.",
        "",
        "  Aucun repere chiffre n'est inscrit ici volontairement. Le",
        "  precedent, mesure le 6 aout 2026, a ete retire le 7 aout : il",
        "  avait ete obtenu avec une fenetre de contexte effective de 2048",
        "  tokens au lieu de 16384, un contexte plafonne en dur a quinze",
        "  extraits, un detecteur d'esquive aveugle au singulier, et sur une",
        "  instance autre que celle qui le publiait. Une valeur absente se",
        "  remarque et se demande ; une valeur fausse se recopie.",
        "",
        "  Le repere de la machine de reference est le paquet de figeage de",
        "  cette machine, accompagne de son rapport_consigne.txt. Les deux",
        "  vont ensemble : des chiffres sans releve de configuration ne",
        "  prouvent rien.",
    ]

    texte = "\n".join(lignes)
    with open(os.path.join(PAQUET, "RELEVE.txt"), "w",
              encoding="utf-8") as f:
        f.write(texte + "\n")

    archive = PAQUET + ".zip"
    if os.path.exists(archive):
        os.remove(archive)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for nom in sorted(os.listdir(PAQUET)):
            z.write(os.path.join(PAQUET, nom),
                    os.path.join(f"paquet_{INSTANCE}", nom))

    print(texte)
    print(f"\nEcrit : {PAQUET}/")
    print(f"Ecrit : {archive}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
