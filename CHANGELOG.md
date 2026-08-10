# Changelog

Toutes les modifications notables de ce projet sont documentées ici.

Le format est basé sur [Keep a Changelog](https://keepachangelog.com/fr/1.0.0/),
et ce projet adhère au [Semantic Versioning](https://semver.org/lang/fr/).

## [Unreleased]

### Fixed

- **`ollama pull` échouait silencieusement derrière un proxy d'entreprise**
  (x509 : certificate signed by unknown authority sur `registry.ollama.ai`).
  Le binaire Go d'Ollama ignore `SSL_CERT_FILE` : le CA d'interception est
  désormais exporté du magasin Windows dans `certs/ollama/ca.crt` et monté
  dans le store système du conteneur (`docker-compose.legacy.yml`, services
  `ollama` et `ollama-cpu`). `INSTALL.ps1` contrôle désormais le code retour
  du `ollama pull` du LLM principal et s'arrête net avec un message clair
  au lieu de poursuivre vers une instance inutilisable.
- **Favicon illisible sur onglets en thème clair** : la luciole blanche sur
  fond transparent était invisible (fond d'onglet gris clair). Nouveau
  `favicon.png` : tuile arrondie en dégradé indigo (`--accent` de l'UI,
  #6366f1 → #8b5cf6) portant la luciole blanche, lisible de 16 à 128 px sur
  fond clair comme sombre. Le Chat UI servait `logo.png` comme favicon ; il
  sert désormais `favicon.png` en priorité (`logo.png` reste utilisé dans
  les pages, sur fond sombre).

- **Interception TLS d'entreprise (proxy/antivirus)** : le téléchargement des
  modèles BGE-M3/reranker (étape 8/9) échouait avec
  `SSLCertVerificationError` sur `huggingface.co`, le CA racine d'interception
  étant absent du bundle certifi des conteneurs. `INSTALL.ps1` exporte
  désormais les certificats racine Windows et `install.sh` réutilise le
  bundle CA de l'hôte ; le bundle est injecté dans le conteneur et désigné
  via `SSL_CERT_FILE` (httpx), `REQUESTS_CA_BUNDLE` (requests) et
  `CURL_CA_BUNDLE` (curl) le temps du téléchargement.
- **Windows AppLocker/WDAC (Constrained Language Mode)** : l'export des
  certificats racine utilisait `[Convert]::ToBase64String()`, bloqué en mode
  de langage contraint. Réécriture avec `Export-Certificate` +
  `certutil -encode` (cmdlets uniquement, aucun appel de méthode .NET).
- `INSTALL.ps1` : arrêt net si `docker build` échoue (contrôle de
  `$LASTEXITCODE`) au lieu d'afficher un faux « Image disponible ».
- Build GPU/CPU/ARM64 : ajout de `download-r2.pytorch.org` (CDN servant
  réellement les wheels torch) aux `--trusted-host` et `PIP_TRUSTED_HOST`.

## [3.0.0] - 2026-07-18

Fusion de `luciole-prime-v2` (base x86/AMD mono-instance) et
`luciole-prime-multi` (ARM64 GX10/DGX Spark GB10, multi-instances) en une base
de code unifiée.

### Added

- Support **ARM64 / NVIDIA Blackwell** (GX10, DGX Spark, GB10, sm_121) :
  `Dockerfile.gpu.arm64`, `GUIDE_INSTALLATION_GX10.md`, scripts `scripts/`
  (`install_gx10.sh`, `prepare_gx10.sh`, `download_model.sh`,
  `download_embeddings.sh`, `list_instances.sh`, `stop_instance.sh`,
  `trt_entrypoint.gx10.sh`).
- Backend **TensorRT-LLM** (Qwen3-30B-A3B-Instruct-2507 NVFP4) derrière le
  contrat OpenAI-compatible `LLM_URL`.
- Architecture **LLM partagé + N instances métier** via le réseau Docker externe
  `luciole_shared` : `docker-compose.shared-llm.yml`,
  `docker-compose.shared-llm.gx10.yml`, `docker-compose.instance.yml`,
  `docker-compose.instance.gx10.yml`.
- Mécanisme **`BUSINESS_PROFILE`** et dossier `config/profiles/` (profils
  `generic`, `eolien`, `horlogerie`, `crm`, `petrochimie`).
- `MIGRATION_GUIDE.md` (v2 → v3) et ce `CHANGELOG.md`.

### Changed

- `BUSINESS_RULES` du query rewriter neutralisé par défaut (`[]`) pour un
  positionnement multi-métier. Les 15 règles éolien / ICPE historiques sont
  archivées dans `config/profiles/query_rewriter.eolien.py`.
- Contrat LLM unifié (`agent/api.py`, `generation/llm.py`, `mail/*`,
  `watcher/config.py`) : Ollama et TensorRT-LLM interchangeables via `LLM_URL`
  (OpenAI-compatible). Tout autre backend OpenAI-compatible (LM Studio, vLLM…)
  reste utilisable comme moteur d'inférence, sans gestion dynamique depuis l'UI.
- `config/settings.yaml.example` : base multi (TensorRT-LLM) + commentaires pour
  bascule Ollama.
- `docker-compose.yml` v2 renommé en `docker-compose.legacy.yml` (déploiement
  mono-instance x86/AMD).
- `chat_ui` lancé via `uvicorn src.api.chat_ui:app` partout (c'est une app
  FastAPI, pas Streamlit).
- Serveur mail de test : **GreenMail** (`greenmail/standalone:latest`).

### Fixed

- **Bloquant** : `SyntaxError` dans `evaluation/ragas_evaluator.py` (paramètre
  sans défaut après paramètre avec défaut) corrigé.
- Correctifs rapatriés depuis `luciole-prime-multi` : `ingestion/embedder.py`,
  `retrieval/reranker.py`, `watcher/index_routing.py`.
- Re-pin `extract-msg==0.48.0` (évite une régression de dépendance).
- `SENTENCE_TRANSFORMERS_HOME` unifié à `/app/models/huggingface`.

### Removed

- Legacy `mail-server/` (Stalwart, non branché) — remplacé par GreenMail.
- Ancien logo `rag-system/pics/luciole.png` (533 Ko, non référencé). Le logo
  officiel est `rag-system/src/api/static/logo.png` (148 Ko) ; le fallback
  pointe désormais vers `pics/luciole-logo.png`.

[3.0.0]: https://github.com/damien148k/luciole-prime-v3/releases/tag/v3.0.0
