# -*- coding: utf-8 -*-
"""
preparer_determinisme.py

Deux choses, dans cet ordre, et rien d'autre.

1. Inventaire. Compare les fichiers Python presents dans le conteneur avec
   ceux de la branche main du depot au commit 9b411a5. La comparaison se
   fait par l'empreinte git du contenu, la meme que celle affichee par
   GitHub, donc aucune ambiguite possible. Sortie : la liste des fichiers
   qui different, manquent ou sont en trop.

2. Configuration du determinisme, seulement avec --appliquer.
   Lu dans rag-system/src/generation/llm.py au commit 9b411a5 :
       self.temperature = llm_config.get("temperature",  0.0)
       self.seed        = llm_config.get("seed",         42)
   Les valeurs par defaut du code ne s'appliquent que si la cle est
   absente de settings.yaml. Le modele de configuration livre avec le
   depot, config/settings.yaml.example, porte temperature: 0.1. Si cette
   ligne est presente dans la configuration de l'instance, reconstruire
   l'image ne change strictement rien a la temperature. Ce script pose
   temperature: 0.0 et seed: 42 dans la section llm, en modifiant les
   lignes concernees et elles seules, en UTF-8, avec une sauvegarde
   horodatee a cote du fichier.

Usage, depuis le dossier de l'instance :
    docker exec luciole-agent-mrae python /app/evaluation/preparer_determinisme.py
    docker exec luciole-agent-mrae python /app/evaluation/preparer_determinisme.py --appliquer

Ce que ce script ne fait pas : il ne reconstruit pas l'image, il ne
redemarre rien, il n'ecrit jamais dans /app/src.
"""

import hashlib
import os
import re
import shutil
import sys
import time

RACINE_SRC = "/app/src"
CONFIG = "/app/config/settings.yaml"
COMMIT = "9b411a5"

# Empreintes git des fichiers .py de rag-system/src sur main au commit 9b411a5.
REFERENCE = {
    "src/__init__.py": "386535dc51b50fd347cf18fc0b5ae2ccd597e637",
    "src/agent/__init__.py": "415dee60301b19a19a886545efdf35c98eeb900d",
    "src/agent/agent_profiles.py": "7a259e80566a3b56cd35b96178add0a3d89caa69",
    "src/agent/analyzer.py": "205a7e8022472b805e44f94f1277935bfe29ebc9",
    "src/agent/api.py": "0564bdbc1cb51f1c5c30b15499de41a664947ccd",
    "src/agent/classifier.py": "d788073e7db237e0bda6d5244ea0256e37afa6fb",
    "src/agent/orchestrator.py": "5f5085b52badb39eaa12248e5445f64e34a718f7",
    "src/agent/tools.py": "8122a75559f797eccc21d52c2cd5193493f50d96",
    "src/api/__init__.py": "2603cda5f72c80b149acd88ff91eb4d3b8ec12e4",
    "src/api/admin_ui.py": "99047cfe18626085f648df666edbf2520edeaf53",
    "src/api/auth.py": "2a71b67028125b341e8bb3bffc0f7ada83df8b62",
    "src/api/chat_ui.py": "4335aeae9a1330f9b63101c097ab5bb85ea0c59b",
    "src/api/feedback_ui.py": "4e9a126020020d5a112e73c8a4941048f1033967",
    "src/api/main.py": "6a2912a23aafe891066f8746e42712ff0ea9c38d",
    "src/config_loader.py": "2771784d049a42d8bb44ce3b99cd69af271acbf3",
    "src/evaluation/__init__.py": "291c2a531146b2b9544c0bb50e97b8e2da1faa1d",
    "src/evaluation/metrics.py": "77a45da3e34ef0bf6aa8690a80643993e955fd27",
    "src/evaluation/metrics_store.py": "ce78f86497aeaba40b31f363ad0f043316849256",
    "src/evaluation/ragas_evaluator.py": "318981a39dbebc869ef7f7b3221dd907f07ac018",
    "src/generation/__init__.py": "9c4e149b191f337108309192f0b7d6edbd942d06",
    "src/generation/llm.py": "be7360d6b37bb06605188e9ba79ad0c72bfae3b0",
    "src/generation/llm_backend.py": "1a95a4aef6ee73bf16ce660801943d6fe2f84c3f",
    "src/ingestion/__init__.py": "f9aa8d9b303dde792a87ded85535c2e14c1e88ed",
    "src/ingestion/chunker.py": "cec125da7097c075eeab1d21d6688d8d660b244e",
    "src/ingestion/embedder.py": "ab7987fe128d274ca4c501179bb8e6b3dcc01347",
    "src/ingestion/excel_parser.py": "2c53837f41534e0e8255ac72c6ccefcd99c9708c",
    "src/ingestion/ocr.py": "dc7a6194cbdb20d1f313e3f3de2a7d532276467c",
    "src/ingestion/parsers.py": "40098686b202977c1fde1cdbda669186c73ee119",
    "src/ingestion/pipeline.py": "daa0326eecb61705d2270b371aa033a22ca999fc",
    "src/ingestion/sql_storage.py": "1d13b4571388df60ed83e219aed2fa5a1d1aedb3",
    "src/mail/__init__.py": "f58ecb952c562e2ea481d7389896a88b505ae36b",
    "src/mail/api.py": "72d70494a3e421c4d6071ec74fafd31566f2ff56",
    "src/mail/approval_service.py": "0fe8d98ddc4330fb7a115105e5779837842ff6e7",
    "src/mail/classifier.py": "707998e9a45146ef1d73debe627ca9bf59bd2020",
    "src/mail/config.py": "8577c525c4bece4c57b757a5fd3b0a8b4ca2767d",
    "src/mail/constants.py": "29ce071bd9bc28157925116b161222993fd20abe",
    "src/mail/db.py": "71203d6c8ac09108a2df025832a3e8a5266733f1",
    "src/mail/draft_service.py": "dc6492e696d66d08de15c371b53ad4c2c0cbc508",
    "src/mail/exceptions.py": "ee1e68e54fae0e7295c35e5185015359d16f4d7c",
    "src/mail/html_renderer.py": "d9867ea61577aa848739cfcdd2cb0e28613960da",
    "src/mail/imap_client.py": "7b491210cacf37c44aead97cb9cb6976ba4a2bc0",
    "src/mail/inbound_service.py": "04ce518ee6fde2bb5e2d3a180f45d5aa39e11ee1",
    "src/mail/models.py": "f7b52e644595639f3bede8b662113b69b363ca9f",
    "src/mail/outbound_service.py": "1003ece90b19d02da1cf74a385ae720ec1e26a71",
    "src/mail/parser.py": "9b8f7ab444c3eaccac994202cbe3d0d87c6dc58f",
    "src/mail/scheduler.py": "9bbe0228bfd26b52611011a5443b2a2bc2b11063",
    "src/mail/smtp_client.py": "382447de67fc51bc1b374a276bc8dc767beb119c",
    "src/mail/state.py": "61d58d84979c3953a0d2d32758cc7b81796098a6",
    "src/retrieval/__init__.py": "9d6135133bf9872ca9ead7f3b5280ca0818452ae",
    "src/retrieval/bm25_search.py": "dfab165646642a80e8a0a378e66cd8d70e936dd3",
    "src/retrieval/dense_search.py": "25d7faee44ab16ff8e5bd0e23343c2dedde0b62b",
    "src/retrieval/hybrid.py": "0d49632ad517de02254554116e2e48e055c29793",
    "src/retrieval/query_engine.py": "1cdfa70e56b680a08fcb91e5c7c3061f29a75656",
    "src/retrieval/query_rewriter.py": "957a6322b2dd93e26edd321bd9e325b32d145683",
    "src/retrieval/reranker.py": "665122054b5e65a6930c39aecc7597b23b8c97d0",
    "src/utils/__init__.py": "8b137891791fe96927ad78e64b0aad7bded08bdc",
    "src/utils/device.py": "3884b26b15cdeedc70cb0b42f0e03a75adfecff7",
    "src/watcher/__init__.py": "27c2128fa3cbefe44ca83e508b51c7c2e2807d90",
    "src/watcher/api.py": "34d5def47e2da1839591430d1cbacb095cea7031",
    "src/watcher/cleanup.py": "e1d5a7417402a096fef4b2153f8073081d418077",
    "src/watcher/config.py": "7db44a4cdbe152abac30c8bbdb4c74b53e658b4c",
    "src/watcher/constants.py": "b4117d190ea3ed1f90bba32c729610bb005ade94",
    "src/watcher/db.py": "e59745c0594685965265183d14fc78b90393fd72",
    "src/watcher/exceptions.py": "d36d04427f0a4f08d270e6c3f99bc1e98a8cb354",
    "src/watcher/hashing.py": "c1ad126de8724d2014a3b25586f96e004bddfbe1",
    "src/watcher/index_routing.py": "f38847147fb0861be9caf1c59acf67c5eca58b5d",
    "src/watcher/main.py": "046b61a92516afa221a4bf5a42132ecab775cf80",
    "src/watcher/models.py": "62bbd1986eb2b2206eef47a3495b8a7a1d341d70",
    "src/watcher/observer.py": "daa2c767469ed9d0c8c6dc4b9229940d2e42596e",
    "src/watcher/queue.py": "f899de95359434d204fc7f6fe817d96269615778",
    "src/watcher/reconciler.py": "36d209fdc9beafddec4ad5f11313a9dea73c607a",
    "src/watcher/service.py": "67743b75115f1ff239c3deb1fa55adc16e22cb7e",
    "src/watcher/state.py": "e7653ead15d34a02ed7858a24838077f58d66286",
    "src/watcher/worker.py": "b3d062da473c5987f83e85c22026268ca845255c",
}

# Fichier monte par docker-compose par dessus la source, il differe de main
# par construction : ./config/query_rewriter.py:/app/src/retrieval/query_rewriter.py
MONTES = {"src/retrieval/query_rewriter.py"}


def empreinte_git(chemin):
    with open(chemin, "rb") as f:
        d = f.read()
    return hashlib.sha1(b"blob " + str(len(d)).encode() + b"\x00" + d).hexdigest()


def inventaire():
    presents = {}
    for dossier, _, fichiers in os.walk(RACINE_SRC):
        if "__pycache__" in dossier:
            continue
        for n in fichiers:
            if not n.endswith(".py"):
                continue
            p = os.path.join(dossier, n)
            presents["src/" + os.path.relpath(p, RACINE_SRC).replace("\\", "/")] = p

    differents, absents, en_trop, montes = [], [], [], []
    for rel, sha in sorted(REFERENCE.items()):
        if rel not in presents:
            absents.append(rel)
        elif empreinte_git(presents[rel]) != sha:
            (montes if rel in MONTES else differents).append(rel)
    for rel in sorted(presents):
        if rel not in REFERENCE:
            en_trop.append(rel)

    print("=" * 78)
    print("INVENTAIRE  conteneur  contre  depot main " + COMMIT)
    print("=" * 78)
    print("  fichiers de reference : %d" % len(REFERENCE))
    print("  fichiers dans /app/src : %d" % len(presents))
    print()
    if not differents and not absents and not en_trop:
        print("  Le conteneur porte deja le code de main. Rien a copier.")
    for titre, liste in (
        ("A REMPLACER, contenu different de main", differents),
        ("MANQUANTS dans le conteneur", absents),
        ("PRESENTS dans le conteneur, absents de main", en_trop),
        ("montes par docker-compose, ecart attendu", montes),
    ):
        if liste:
            print("  %s : %d" % (titre, len(liste)))
            for rel in liste:
                print("      %s" % rel)
            print()
    return differents, absents, en_trop


def _bloc(chemin, entete):
    """Corps d'une fonction ou d'une methode, lu dans le fichier source.

    Lecture de texte, sans import : importer src.agent.api construirait
    l'application FastAPI dans ce processus, avec ses effets de bord.
    """
    try:
        texte = open(chemin, encoding="utf-8").read()
    except Exception:
        return None
    i = texte.find(entete)
    if i < 0:
        return None
    reste = texte[i + len(entete):]
    fins = [reste.find("\ndef "), reste.find("\n@app"), reste.find("\n    def ")]
    fins = [x for x in fins if x >= 0]
    return entete + (reste[:min(fins)] if fins else reste)


def marqueurs():
    """Trois marqueurs, chacun lu a l'endroit exact qui produit l'effet.

    Le premier controle du reecriveur cherchait la chaine query_rewriter
    dans tout orchestrator.py. Faux positif garanti : sur main, la classe
    AgentOrchestrator garde un parametre query_rewriter optionnel, ligne
    104, et le pipeline procedural s'en sert. Ce n'est pas la que se joue
    l'injection. Elle se joue dans get_orchestrator de src/agent/api.py,
    la seule fonction qui construit l'orchestrateur de la boucle : sur
    main au commit 7dbdbac, elle ne passe plus l'argument. C'est donc ce
    bloc, et lui seul, qui est examine ici.
    """
    print("=" * 78)
    print("MARQUEURS, LUS DANS LES FICHIERS DU CONTENEUR")
    print("=" * 78)

    llm = "/app/src/generation/llm.py"
    bloc = _bloc(llm, "    def _extract_sources(")
    texte = ""
    try:
        texte = open(llm, encoding="utf-8").read()
    except Exception as e:
        print("  lecture de %s impossible : %s" % (llm, e))
    if texte:
        print("  germe transmis dans la charge utile   :",
              'payload["seed"]' in texte or "payload['seed']" in texte)
    if bloc is None:
        print("  _extract_sources introuvable dans llm.py")
    else:
        print("  pages recopiees dans _extract_sources :", "page_start" in bloc)

    api = "/app/src/agent/api.py"
    bloc = _bloc(api, "def get_orchestrator(")
    if bloc is None:
        print("  get_orchestrator introuvable dans api.py")
    else:
        injecte = bool(re.search(r"query_rewriter\s*=", bloc))
        print("  reecriveur injecte dans la boucle     :", injecte)
    print()


def config_llm():
    print("=" * 78)
    print("SECTION llm DE %s" % CONFIG)
    print("=" * 78)
    try:
        import yaml
        c = yaml.safe_load(open(CONFIG, encoding="utf-8")) or {}
    except Exception as e:
        print("  lecture impossible :", e)
        return None
    llm = c.get("llm", {}) or {}
    for cle in ("provider", "model", "base_url", "temperature", "seed",
                "max_tokens", "num_ctx", "timeout"):
        val = llm.get(cle, "<absente>")
        note = ""
        if cle == "temperature" and val not in ("<absente>", 0, 0.0):
            note = "   <-- le determinisme demande 0.0"
        if cle == "temperature" and val == "<absente>":
            note = "   (le code prend 0.0 par defaut)"
        if cle == "seed" and val == "<absente>":
            note = "   (le code prend 42 par defaut)"
        print("  %-12s %s%s" % (cle, val, note))
    print()
    return llm


def appliquer():
    lignes = open(CONFIG, encoding="utf-8").read().split("\n")
    debut = None
    for i, l in enumerate(lignes):
        if re.match(r"^llm:\s*(#.*)?$", l):
            debut = i
            break
    if debut is None:
        print("  Aucune section llm: trouvee dans %s. Rien modifie." % CONFIG)
        return False
    fin = len(lignes)
    for i in range(debut + 1, len(lignes)):
        l = lignes[i]
        if l.strip() and not l.startswith((" ", "\t")):
            fin = i
            break

    voulu = {"temperature": "0.0", "seed": "42"}
    vus, modifs = set(), []
    for i in range(debut + 1, fin):
        m = re.match(r"^(\s*)([A-Za-z_]+)(\s*:\s*)(.*?)(\s*(?:#.*)?)$", lignes[i])
        if not m:
            continue
        indent, cle, sep, val, queue = m.groups()
        if cle in voulu:
            vus.add(cle)
            if val.strip() != voulu[cle]:
                modifs.append((i + 1, cle, val.strip(), voulu[cle]))
                lignes[i] = "%s%s%s%s%s" % (indent, cle, sep, voulu[cle], queue)

    # Insertion apres la derniere ligne non vide du bloc, pour ne pas
    # deposer la cle apres la ligne blanche qui separe deux sections.
    point = fin
    for i in range(fin - 1, debut, -1):
        if lignes[i].strip():
            point = i + 1
            break

    ajouts = []
    for cle in ("temperature", "seed"):
        if cle not in vus:
            note = ("  # determinisme : germe fixe transmis a chaque appel"
                    if cle == "seed" else
                    "  # determinisme : pas d'echantillonnage")
            lignes.insert(point, "  %s: %s%s" % (cle, voulu[cle], note))
            point += 1
            fin += 1
            ajouts.append(cle)

    if not modifs and not ajouts:
        print("  temperature et seed sont deja aux valeurs voulues. Rien ecrit.")
        return False

    sauvegarde = CONFIG + ".avant-determinisme-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(CONFIG, sauvegarde)
    with open(CONFIG, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lignes))

    print("  sauvegarde : %s" % sauvegarde)
    for numero, cle, avant, apres in modifs:
        print("  ligne %d : %s %s -> %s" % (numero, cle, avant, apres))
    for cle in ajouts:
        print("  ajoute : %s: %s" % (cle, voulu[cle]))
    print()
    import yaml
    c = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    llm = c.get("llm", {})
    print("  relecture : temperature=%r seed=%r" % (llm.get("temperature"),
                                                    llm.get("seed")))
    print("  Le fichier est monte depuis l'hote : redemarre le conteneur")
    print("  agent pour que la valeur soit prise en compte.")
    return True


def main():
    inventaire()
    marqueurs()
    config_llm()
    if "--appliquer" in sys.argv:
        print("=" * 78)
        print("ECRITURE DE LA CONFIGURATION")
        print("=" * 78)
        appliquer()
    else:
        print("Relance avec --appliquer pour poser temperature: 0.0 et seed: 42.")


if __name__ == "__main__":
    main()
