# Changelog

Toutes les modifications notables de ce projet sont documentées ici.

Le format est basé sur [Keep a Changelog](https://keepachangelog.com/fr/1.0.0/),
et ce projet adhère au [Semantic Versioning](https://semver.org/lang/fr/).

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
  `watcher/config.py`) : Ollama, LM Studio et TensorRT-LLM interchangeables.
- `config/settings.yaml.example` : base multi (TensorRT-LLM) + commentaires pour
  bascule Ollama / LM Studio.
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
