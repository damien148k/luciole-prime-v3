# Guide d'installation — Luciole Prime Multi (GX10)

> **Machine cible** : NVIDIA DGX Spark GX10 — Grace Blackwell GB10, arm64, sm_121  
> **Utilisateur terminal** : `dam@gx10-ca25`  
> **Repo** : `luciole-prime-multi` (privé)  
> **Objectif** : Déployer Luciole multi-instances avec LLM TRT-LLM partagé

---

## Sommaire

1. [Premier démarrage et mise à jour du GX10](#1-premier-démarrage-et-mise-à-jour-du-gx10)
2. [Docker — permissions et groupe](#2-docker--permissions-et-groupe)
3. [NVIDIA Container Runtime — configuration Docker](#3-nvidia-container-runtime--configuration-docker)
4. [Compte NGC personnel et API Key](#4-compte-ngc-personnel-et-api-key)
5. [Login Docker sur nvcr.io](#5-login-docker-sur-nvcrIo)
6. [Cloner le repo luciole-prime-multi](#6-cloner-le-repo-luciole-prime-multi)
7. [Téléchargement des embeddings (venv Python)](#7-téléchargement-des-embeddings-venv-python)
8. [Téléchargement du modèle LLM (Qwen3-30B-A3B-NVFP4)](#8-téléchargement-du-modèle-llm-qwen3-30b-a3b-nvfp4)
9. [Build de l'image Docker GPU arm64](#9-build-de-limage-docker-gpu-arm64)
10. [Démarrage du stack LLM partagé](#10-démarrage-du-stack-llm-partagé)
11. [Installation d'une instance métier](#11-installation-dune-instance-métier)
12. [Vérification finale et tests](#12-vérification-finale-et-tests)
13. [Gestion des instances](#13-gestion-des-instances)
14. [Erreurs courantes et solutions](#14-erreurs-courantes-et-solutions)

---

## 1. Premier démarrage et mise à jour du GX10

Au premier démarrage, mettre à jour le système avant toute manipulation Docker ou NGC.

```bash
sudo apt-get update && sudo apt-get upgrade -y
sudo reboot
```

Après redémarrage, vérifier l'architecture et la version du système :

```bash
uname -m          # doit afficher : aarch64
cat /etc/os-release
```

### Accès distant depuis un autre PC (SSH)

Pour travailler depuis un PC Windows/Mac sans être physiquement sur le GX10 :

**Sur le GX10** — activer SSH :
```bash
sudo systemctl enable ssh --now
```

**Depuis le PC distant** (PowerShell / Terminal Windows) :
```powershell
ssh dam@192.168.1.14
```

> Ne pas copier-coller les deux blocs en même temps — ils s'exécutent sur des machines différentes.

Trouver l'IP du GX10 :

```bash
ip addr show | grep "192.168"
# ex : 192.168.1.14
```

---

## 2. Docker — permissions et groupe

### Problème rencontré

```
permission denied while trying to connect to the Docker API at unix:///var/run/docker.sock
```

> **Cause** : l'utilisateur `dam` n'appartient pas au groupe `docker`.

### Solution (permanente)

```bash
# Ajouter l'utilisateur au groupe docker
sudo usermod -aG docker $USER

# Appliquer sans logout (session courante uniquement)
newgrp docker

# Vérifier
docker ps
```

> **Important** : `newgrp docker` ne vaut que pour la session courante.  
> Pour que ce soit permanent, déconnecter/reconnecter ou redémarrer la machine.  
> En attendant, préfixer toutes les commandes Docker avec `sudo`.

### Vérification des permissions du socket

```bash
ls -l /var/run/docker.sock
# Attendu : srw-rw---- 1 root docker ...
```

Si les permissions sont incorrectes :

```bash
sudo chown root:docker /var/run/docker.sock
sudo chmod 660 /var/run/docker.sock
sudo systemctl restart docker
```

---

## 3. NVIDIA Container Runtime — configuration Docker

### Problème rencontré

```
ImportError: libcuda.so.1: cannot open shared object file: No such file or directory
```

> **Cause** : le runtime Docker par défaut est `runc` au lieu de `nvidia`.  
> `NVIDIA_VISIBLE_DEVICES=all` dans les variables d'environnement n'a aucun effet sans le runtime NVIDIA.  
> `DeviceRequests: null` dans `docker inspect` confirme que le GPU n'est pas injecté.

### Vérification

```bash
sudo docker info | grep -i runtime
# Si "Default Runtime: runc" → appliquer la correction ci-dessous

which nvidia-container-runtime
# Doit retourner : /usr/bin/nvidia-container-runtime
```

### Solution

```bash
# Créer le daemon.json Docker avec nvidia comme runtime par défaut
sudo tee /etc/docker/daemon.json <<'EOF'
{
  "default-runtime": "nvidia",
  "runtimes": {
    "nvidia": {
      "path": "nvidia-container-runtime",
      "runtimeArgs": []
    }
  }
}
EOF

# Redémarrer Docker
sudo systemctl restart docker

# Vérifier
sudo docker info | grep -i runtime
# Attendu : Default Runtime: nvidia
```

> **Note** : cette étape est **obligatoire avant tout `docker compose up`** impliquant le GPU.  
> Sans elle, `NVIDIA_VISIBLE_DEVICES=all` est ignoré et `libcuda.so.1` est introuvable.

---

## 4. Compte NGC personnel et API Key

### Problème rencontré

Sur le portail NGC, lors de la génération d'une API key, le bandeau suivant apparaît :

```
API Access Restricted by your Organization.
```

> **Cause** : le compte NVIDIA/NGC est rattaché à une organisation qui bloque la génération d'API key.

### Solution

Créer un compte NGC **personnel** séparé, non rattaché à une organisation.

1. Aller sur [https://ngc.nvidia.com](https://ngc.nvidia.com)
2. Se connecter ou créer un **nouveau compte personnel** avec une adresse email personnelle (ex: Gmail)
3. S'assurer que ce compte n'est lié à **aucune organisation**
4. Dans le menu du compte : **Setup → Generate API Key → + Generate Personal Key**
5. Copier la clé générée et la stocker dans un gestionnaire de mots de passe — **elle ne s'affiche qu'une seule fois**

---

## 5. Login Docker sur nvcr.io

### Problème rencontré

```
Get "https://nvcr.io/v2/": unauthorized
```

> **Cause** : le registre NGC n'accepte pas l'email comme identifiant Docker.  
> Il faut obligatoirement utiliser le token technique `$oauthtoken`.

### Procédure correcte

```bash
# Le username est LITTÉRALEMENT "$oauthtoken" — ne pas le remplacer
sudo docker login nvcr.io --username '$oauthtoken'
# Password: <COLLER_LA_CLE_API_NGC_PERSONNELLE>
```

> **Attention** : utiliser des guillemets simples autour de `$oauthtoken` pour  
> éviter que le shell l'interprète comme une variable vide.

Le résultat attendu : `Login Succeeded`

> **Note** : utiliser systématiquement `sudo docker login` pour que `sudo docker pull` fonctionne.

---

## 6. Cloner le repo luciole-prime-multi

> **Réinstallation from scratch** : si le dossier existe déjà (appartenant à root après une ancienne installation), le supprimer d'abord :
> ```bash
> sudo rm -rf ~/Documents/luciole-prime-multi
> ```

```bash
cd ~/Documents
git clone git@github.com:damien148k/luciole-prime-multi.git
cd luciole-prime-multi
```

> **Prérequis** : la clé SSH du GX10 doit être ajoutée sur GitHub.  
> Si ce n'est pas fait :
> ```bash
> ssh-keygen -t ed25519 -C "gx10-ca25"
> cat ~/.ssh/id_ed25519.pub
> # Copier la clé → GitHub → Settings → SSH Keys → New SSH Key
> ```

Structure du repo :

```
luciole-prime-multi/
├── docker-compose.shared-llm.yml       # Stack LLM partagé (base)
├── docker-compose.shared-llm.gx10.yml  # Override GPU GX10
├── docker-compose.instance.yml         # Stack instance métier (base)
├── docker-compose.instance.gx10.yml    # Override GPU par instance
├── Dockerfile.gpu.arm64                # Image RAG arm64
├── rag-system/                         # Code Python (agent, chat, admin, watcher, mail)
├── config/                             # Configuration YAML
├── scripts/
│   ├── install_gx10.sh                 # Installeur interactif
│   ├── prepare_gx10.sh                 # Prépare embeddings + CUTLASS config
│   ├── download_model.sh               # Télécharge le modèle LLM
│   ├── trt_entrypoint.gx10.sh         # Entrypoint TRT-LLM GX10
│   ├── stop_instance.sh
│   └── list_instances.sh
├── instances/                          # Créé par install_gx10.sh
│   └── <metier>/
│       ├── .env
│       ├── data/                       # Documents à ingérer
│       ├── config/
│       └── feedbacks/
├── models/
│   ├── huggingface/                    # Embeddings partagés (bge-m3, bge-reranker-v2-m3)
│   └── hf_models/                     # Modèle LLM (Qwen3-30B-A3B-NVFP4)
└── README.md
```

---

## 7. Téléchargement des embeddings (venv Python)

### Problème rencontré

```
error: externally-managed-environment
```

> **Cause** : sur Debian/Ubuntu récents (PEP 668), `pip install` système est bloqué.  
> Le script utilise un venv Python dédié pour contourner ce blocage.

### Solution

```bash
cd ~/Documents/luciole-prime-multi

# Exécuter avec sudo bash (chemin explicite obligatoire)
sudo bash scripts/prepare_gx10.sh
```

> **Erreur classique** : `sudo prepare_gx10.sh` → `commande introuvable`  
> **Toujours** utiliser `sudo bash scripts/<nom_du_script>.sh`

Ce script :
- Crée le dossier `models/huggingface/` pour les embeddings partagés
- Crée un venv Python dans `~/luciole-venv/` si absent
- Télécharge `bge-m3` et `bge-reranker-v2-m3` depuis HuggingFace
- Configure `extra-llm-api-config.yml` avec `moe_config: backend: CUTLASS`
- Ajuste les permissions (`chown`)

---

## 8. Téléchargement du modèle LLM (Qwen3-30B-A3B-NVFP4)

Le modèle est téléchargé depuis HuggingFace dans `models/hf_models/`.  
Il est **partagé entre toutes les instances** — ne télécharger qu'une seule fois.

```bash
cd ~/Documents/luciole-prime-multi

# Activer le venv créé par prepare_gx10.sh
source ~/luciole-venv/bin/activate

# Télécharger le modèle
bash scripts/download_model.sh
```

> Le modèle Qwen3-30B-A3B-Instruct-2507-NVFP4 est volumineux (~15-20 Go).  
> Le téléchargement peut prendre 30 à 60 minutes selon la connexion.

---

## 9. Build de l'image Docker GPU arm64

L'image `luciole-gpu:arm64` est buildée nativement sur le GX10.  
Elle contient tout le code Python RAG et est partagée par toutes les instances.

```bash
cd ~/Documents/luciole-prime-multi

# Build (prend 10-20 minutes au premier build)
sudo docker build -f Dockerfile.gpu.arm64 -t luciole-gpu:arm64 .
```

> **Quand rebuilder** : uniquement si du code Python dans `rag-system/src/` a été modifié  
> ou si `requirements-linux-gpu.txt` a changé. Les modifications de `docker-compose*.yml`  
> ou de scripts ne nécessitent **pas** de rebuild.

> **Rebuild forcé** (sans cache) :  
> `sudo docker build --no-cache -f Dockerfile.gpu.arm64 -t luciole-gpu:arm64 .`

---

## 10. Démarrage du stack LLM partagé

Le LLM TRT-LLM est démarré **une seule fois** et reste actif pour toutes les instances.

```bash
cd ~/Documents/luciole-prime-multi

sudo docker compose \
  -f docker-compose.shared-llm.yml \
  -f docker-compose.shared-llm.gx10.yml \
  up -d
```

Surveiller les logs :

```bash
sudo docker logs -f luciole-tensorrt-shared
```

Le modèle est prêt quand les logs affichent :

```
Started HTTPService...
```

Vérification :

```bash
curl -s http://localhost:8001/v1/models | python3 -m json.tool
```

> **Note** : le healthcheck est configuré avec `start_period: 600s` — Docker affichera  
> `starting` pendant jusqu'à 10 minutes, c'est normal.

---

## 11. Installation d'une instance métier

Une fois le stack LLM partagé démarré et healthy :

```bash
cd ~/Documents/luciole-prime-multi

sudo bash scripts/install_gx10.sh
```

Le script est interactif :

```
Pour quel métier / client ? (ex: juridique, chavenay, monclient) : support

Ports assignés à l'instance 'support' :
   API (agent)    : 8010
   Admin UI       : 8011
   Chat UI        : 8012
   Feedback UI    : 8013
   Qdrant         : 8014
   OpenSearch     : 8015
   Watcher        : 8016
   Mail SMTP      : 8017
   Mail IMAP      : 8018
   Mail Admin     : 8019
```

Le script :
1. Valide que le réseau `luciole_shared` existe (LLM partagé actif)
2. Détecte automatiquement les ports libres (blocs de 10 par instance)
3. Crée `instances/<metier>/` avec les sous-dossiers data, config, feedbacks, backups
4. Génère `instances/<metier>/.env` avec tous les ports et la clé de chiffrement mail
5. Lance le stack Docker de l'instance avec `--profile gpu`

### Donner les droits sur le dossier de l'instance

Tous les sous-dossiers de l'instance sont créés par Docker (root).  
Il faut donner les droits à l'utilisateur pour pouvoir y déposer des documents et écrire dans les configs :

```bash
sudo chown -R dam:dam ~/Documents/luciole-prime-multi/instances/<metier>/
```

### Installer une deuxième instance

Relancer le script — il détecte automatiquement les ports déjà utilisés :

```bash
sudo bash scripts/install_gx10.sh
# Répondre : juridique → ports 8020-8029 auto-assignés
```

---

## 12. Vérification finale et tests

### Vérifier tous les containers

```bash
sudo docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

Résultat attendu (exemple instance support, ports 8010-8019) :

```
luciole-tensorrt-shared         Up X minutes (healthy)
luciole-qdrant-support          Up X minutes
luciole-opensearch-support      Up X minutes
luciole-agent-support           Up X minutes
luciole-admin-support           Up X minutes
luciole-chat-support            Up X minutes
luciole-feedback-support        Up X minutes
luciole-watcher-support         Up X minutes
luciole-mail-support            Up X minutes (healthy)
```

### Test du LLM partagé

```bash
curl -s http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3-30b-a3b-instruct","messages":[{"role":"user","content":"Bonjour"}],"max_tokens":50}' \
  | python3 -m json.tool
```

### Test de l'API RAG

```bash
curl -s http://localhost:8010/health
# {"status": "ok", "instance": "support"}
```

### Accès aux interfaces (local ou réseau)

| Interface | URL locale | URL réseau |
|---|---|---|
| Chat UI | `http://localhost:8012` | `http://<IP_GX10>:8012` |
| Admin UI | `http://localhost:8011` | `http://<IP_GX10>:8011` |
| Feedback / Mail | `http://localhost:8013` | `http://<IP_GX10>:8013` |

### Création du compte GreenMail (service mail)

Après le démarrage des containers, créer le compte mail local utilisé par Luciole.  
Récupérer le port admin GreenMail depuis le `.env` de l'instance :

```bash
source ~/Documents/luciole-prime-multi/instances/<metier>/.env
curl -s -X POST http://localhost:${MAIL_ADMIN_PORT}/api/user \
  -H "Content-Type: application/json" \
  -d '{"email":"luciole@luciole.local","login":"luciole","password":"luciole"}'
# Réponse attendue : {"login":"luciole","email":"luciole@luciole.local"}
```

Configurer le client mail externe (Outlook, Thunderbird...) :

| Paramètre | Valeur |
|---|---|
| Email | `luciole@luciole.local` |
| Login | `luciole` |
| Mot de passe | `luciole` |
| IMAP host | `<IP_GX10>` |
| IMAP port | `MAIL_IMAP_PORT` (ex: 8018) — **sans chiffrement** |
| SMTP host | `<IP_GX10>` |
| SMTP port | `MAIL_SMTP_PORT` (ex: 8017) — **sans chiffrement** |

Configurer aussi IMAP/SMTP dans l'UI Feedback → Settings mail (même valeurs, host = `mail`).

> **Note** : le compte GreenMail est perdu à chaque `docker rm` du container mail. À recréer après chaque recréation du container.

### Ingestion de documents

Déposer les documents dans le dossier de l'instance :

```bash
# ⚠️  RÈGLE CRITIQUE : le sous-dossier doit porter EXACTEMENT le même nom que l'instance
# Exemple pour l'instance "support" : instances/support/data/support/
# Exemple pour l'instance "juridique" : instances/juridique/data/juridique/
mkdir -p ~/Documents/luciole-prime-multi/instances/<metier>/data/<metier>/
cp /path/to/documents/*.pdf \
   ~/Documents/luciole-prime-multi/instances/<metier>/data/<metier>/
```

> **Pourquoi** : le nom du sous-dossier détermine l'index Qdrant. Si le nom ne correspond pas à `INSTANCE_NAME`,  
> l'agent ne trouvera aucun résultat (0 vecteurs) et le watcher ne surveillera pas le bon chemin.
>
> ❌ `instances/support/data/chavenay/` → index `chavenay` — l'agent cherche `support` → 0 résultats  
> ✅ `instances/support/data/support/` → index `support` — correct

Depuis l'UI Admin, lancer l'ingestion avec le chemin `/app/data/<metier>`.

Le watcher surveille automatiquement les changements (ajout/suppression) toutes les **60 secondes**.

---

## 13. Démarrage après reboot

Après un redémarrage du GX10, Docker redémarre automatiquement mais le LLM partagé doit être relanceé manuellement car il nécessite un temps de warmup.

```bash
cd ~/Documents/luciole-prime-multi

# 1. Démarrer le LLM partagé
sudo docker compose \
  -f docker-compose.shared-llm.yml \
  -f docker-compose.shared-llm.gx10.yml \
  up -d

# 2. Surveiller le démarrage (jusqu'à 10 min)
sudo docker logs -f luciole-tensorrt-shared
# Attendre l'un de ces messages puis Ctrl+C :
#   - "Started HTTPService"  (premier démarrage, avec warmup)
#   - "Application startup complete" + réponses "GET /v1/models HTTP/1.1" 200 OK (démarrage rapide depuis cache)

# 3. Démarrer chaque instance
cd instances/<metier>
sudo docker compose -f docker-compose.yml -f docker-compose.gx10.yml \
  --project-name luciole-<metier> --profile gpu up -d
```

> Les containers d'instance (qdrant, opensearch, mail...) redémarrent automatiquement  
> grâce à `restart: unless-stopped`. Seul le LLM partagé doit être relanceé manuellement.

---

## 14. Gestion des instances

### Lister les instances

```bash
sudo bash scripts/list_instances.sh
```

### Arrêter une instance

```bash
sudo bash scripts/stop_instance.sh support
```

### Arrêter le stack LLM partagé

> **Attention** : arrêter le LLM partagé interrompt **toutes** les instances actives.

```bash
cd ~/Documents/luciole-prime-multi
sudo docker compose \
  -f docker-compose.shared-llm.yml \
  -f docker-compose.shared-llm.gx10.yml \
  down
```

### Recréer un ou plusieurs containers

```bash
# IMPORTANT : toujours inclure --profile gpu
# sinon les services GPU démarrent sur un réseau séparé (DNS failure)
cd ~/Documents/luciole-prime-multi/instances/<metier>
sudo docker stop luciole-<service>-<metier> && sudo docker rm luciole-<service>-<metier>
sudo docker compose -f docker-compose.yml -f docker-compose.gx10.yml \
  --project-name luciole-<metier> --profile gpu up -d
```

### Recréer TOUS les containers d'une instance

```bash
cd ~/Documents/luciole-prime-multi/instances/<metier>
sudo docker stop luciole-qdrant-<metier> luciole-opensearch-<metier> \
  luciole-agent-<metier> luciole-admin-<metier> luciole-chat-<metier> \
  luciole-feedback-<metier> luciole-watcher-<metier> luciole-mail-<metier>
sudo docker rm luciole-qdrant-<metier> luciole-opensearch-<metier> \
  luciole-agent-<metier> luciole-admin-<metier> luciole-chat-<metier> \
  luciole-feedback-<metier> luciole-watcher-<metier> luciole-mail-<metier>
sudo docker compose -f docker-compose.yml -f docker-compose.gx10.yml \
  --project-name luciole-<metier> --profile gpu up -d
```

> Ne pas oublier de recréer le compte GreenMail après (voir section 12).

### Mettre à jour le code (git pull + rebuild)

```bash
cd ~/Documents/luciole-prime-multi
git pull

# Rebuild image (uniquement si code Python modifié)
sudo docker build -f Dockerfile.gpu.arm64 -t luciole-gpu:arm64 .

# Recréer les containers GPU (pas qdrant/opensearch/mail)
cd instances/<metier>
sudo docker stop luciole-agent-<metier> luciole-admin-<metier> \
  luciole-chat-<metier> luciole-feedback-<metier> luciole-watcher-<metier>
sudo docker rm luciole-agent-<metier> luciole-admin-<metier> \
  luciole-chat-<metier> luciole-feedback-<metier> luciole-watcher-<metier>
sudo docker compose -f docker-compose.yml -f docker-compose.gx10.yml \
  --project-name luciole-<metier> --profile gpu up -d
```

---

## 14. Erreurs courantes et solutions

### `permission denied while trying to connect to the Docker API`

```bash
sudo usermod -aG docker $USER && newgrp docker
```

### `sudo: download_model.sh: commande introuvable`

Toujours utiliser `sudo bash scripts/<nom>.sh` avec le chemin explicite.

### `Get "https://nvcr.io/v2/": unauthorized`

Utiliser `$oauthtoken` (littéral) comme username, avec des guillemets simples :
```bash
sudo docker login nvcr.io --username '$oauthtoken'
```

### `error: externally-managed-environment` (pip)

Ne pas utiliser `sudo pip install`. Passer par le venv :
```bash
source ~/luciole-venv/bin/activate && pip install <package>
```

### `libcuda.so.1: cannot open shared object file`

Le runtime Docker n'est pas nvidia. Vérifier et corriger (voir section 3) :
```bash
sudo docker info | grep "Default Runtime"
# Si runc → appliquer daemon.json
```

### `Temporary failure in name resolution` (DNS Docker)

Les containers ne sont pas tous sur le même réseau Docker.  
**Cause** : commande `docker compose up` lancée sans `--profile gpu` ou sans `--project-name`.  
**Solution** : recréer tous les containers ensemble (voir "Recréer TOUS les containers").

### `--force-recreate` garde l'ancienne image

`--force-recreate` recrée le container mais réutilise l'image en cache.  
Toujours faire `docker stop + docker rm` puis `up -d --profile gpu`.

### Modèle BAAI/bge-m3 non trouvé

Vérifier que `HF_MODELS_PATH` est dans le `.env` de l'instance :
```bash
grep HF_MODELS_PATH ~/Documents/luciole-prime-multi/instances/<metier>/.env
# Si absent :
echo "HF_MODELS_PATH=/home/dam/Documents/luciole-prime-multi/models/huggingface" \
  >> ~/Documents/luciole-prime-multi/instances/<metier>/.env
# Puis recréer les containers GPU
```

### Index Qdrant vide (0 résultats au chat)

Le nom du dossier source ≠ nom de l'instance.  
Le dossier doit s'appeler exactement comme le métier : `data/<metier>/`.

### Compte GreenMail perdu après `docker rm`

Recréer le compte :
```bash
source ~/Documents/luciole-prime-multi/instances/<metier>/.env
curl -s -X POST http://localhost:${MAIL_ADMIN_PORT}/api/user \
  -H "Content-Type: application/json" \
  -d '{"email":"luciole@luciole.local","login":"luciole","password":"luciole"}'
```

### Permission refusée sur `instances/<metier>/`

```bash
sudo chown -R dam:dam ~/Documents/luciole-prime-multi/instances/<metier>/
```

---

## Récapitulatif — Ordre d'installation

```
1.  sudo apt update && upgrade → reboot
2.  sudo systemctl enable ssh --now   ← accès distant
3.  sudo usermod -aG docker $USER → newgrp docker
4.  Configurer NVIDIA Container Runtime → daemon.json → sudo systemctl restart docker
5.  Créer compte NGC personnel → générer API key
6.  sudo docker login nvcr.io --username '$oauthtoken'
7.  git clone git@github.com:damien148k/luciole-prime-multi.git
8.  sudo bash scripts/prepare_gx10.sh              ← embeddings + CUTLASS config
9.  source ~/luciole-venv/bin/activate && bash scripts/download_model.sh  ← modèle LLM
10. sudo docker build -f Dockerfile.gpu.arm64 -t luciole-gpu:arm64 .
11. sudo docker compose -f docker-compose.shared-llm.yml \
      -f docker-compose.shared-llm.gx10.yml up -d  ← LLM partagé
12. (attendre que luciole-tensorrt-shared soit healthy — jusqu'à 10 min)
13. sudo bash scripts/install_gx10.sh              ← première instance métier
14. sudo chown -R dam:dam instances/<metier>/data/  ← droits sur le dossier data
15. Créer compte GreenMail (curl POST /api/user)
16. Configurer client mail externe (Outlook/Thunderbird)
17. Déposer documents dans instances/<metier>/data/<metier>/
18. Lancer ingestion depuis Admin UI → /app/data/<metier>
19. Tester : Chat UI, watcher (ajout/suppression), mail entrant → brouillon
```

---

*Guide généré le 2026-07-04 — basé sur les difficultés réelles rencontrées lors de l'installation initiale du GX10 (dam@gx10-ca25). Tous les problèmes documentés ici ont été reproduits et résolus.*
