# Guide de migration v2 → v3

Ce guide décrit le passage de **Luciole Prime v2** (x86/AMD, mono-instance,
backend Ollama) vers **Luciole Prime v3**, qui unifie dans une seule base de
code la mono-instance x86 et l'architecture ARM64 multi-instances (GX10 / DGX
Spark / GB10, backend TensorRT-LLM).

La v3 est **rétro-compatible** avec un déploiement mono-instance x86 : si vous
ne voulez rien changer, utilisez `docker-compose.legacy.yml`.

---

## 1. Ce qui ne change pas

- Le pipeline RAG (ingestion → embeddings → recherche hybride → reranking →
  génération) est identique.
- Les formats de documents supportés sont inchangés.
- La configuration `config/settings.yaml`, `prompts.yaml`, `synonyms.txt` reste
  compatible.
- Le déploiement x86/AMD mono-instance avec Ollama fonctionne toujours.

## 2. Ce qui change

| Sujet | v2 | v3 |
|---|---|---|
| Fichier compose mono-instance | `docker-compose.yml` | `docker-compose.legacy.yml` |
| Backend LLM | Ollama uniquement | Ollama / LM Studio / TensorRT-LLM (contrat `LLM_URL`) |
| Architecture ARM64 | ✗ | GX10 / GB10 (Blackwell sm_121) |
| Multi-instances métier | ✗ | LLM partagé + N instances (réseau `luciole_shared`) |
| Règles de query rewriting | 15 règles éolien codées en dur | `BUSINESS_RULES = []` par défaut + profils `config/profiles/` |
| Serveur mail de test | `mail-server/` (Stalwart, non branché) | GreenMail (`greenmail/standalone:latest`) |
| Lancement chat UI | `uvicorn src.api.chat_ui:app` | idem (`chat_ui` est une app FastAPI) |
| Logo | `pics/luciole.png` (533 Ko) | `src/api/static/logo.png` (148 Ko) |

## 3. Migration d'une instance x86/AMD existante

1. Récupérez la v3 :
   ```bash
   git clone https://github.com/damien148k/luciole-prime-v3.git
   cd luciole-prime-v3
   ```
2. Reprenez votre `docker-compose.yml` v2 : il correspond désormais à
   `docker-compose.legacy.yml`. Reportez vos éventuelles personnalisations
   (ports, volumes, variables) dans ce fichier.
3. Copiez votre `config/settings.yaml` existant. Le backend Ollama reste
   configurable — voir les commentaires « Backends alternatifs » dans
   `config/settings.yaml.example`.
4. Démarrez :
   ```bash
   docker compose -f docker-compose.legacy.yml up -d
   ```

### Règles métier éolien

Si votre instance v2 s'appuyait sur les 15 règles éolien / ICPE, elles ont été
archivées dans `config/profiles/query_rewriter.eolien.py`. Pour les réactiver :

```bash
export BUSINESS_PROFILE=eolien
```

Voir `config/profiles/README.md` pour le mécanisme complet.

## 4. Passer à l'architecture ARM64 multi-instances (GX10 / GB10)

Nouveau déploiement (pas une migration in-place) :

1. Suivez [GUIDE_INSTALLATION_GX10.md](GUIDE_INSTALLATION_GX10.md).
2. Créez le réseau partagé et démarrez le backend LLM :
   ```bash
   docker network create luciole_shared
   docker compose -f docker-compose.shared-llm.gx10.yml up -d
   ```
3. Démarrez une instance métier :
   ```bash
   INSTANCE_NAME=eolien BUSINESS_PROFILE=eolien \
     docker compose -f docker-compose.instance.gx10.yml up -d
   ```
4. Répétez l'étape 3 pour chaque métier (`horlogerie`, `crm`, `petrochimie`…),
   chacun avec son `INSTANCE_NAME`, son bloc de ports et son `BUSINESS_PROFILE`.

## 5. Vérifications post-migration

- Le chat répond : `http://<host>:<CHAT_PORT>`
- L'API agent répond : `http://<host>:<AGENT_PORT>/health`
- Les documents sont ré-indexés (le watcher reconcilie au démarrage).
- Le backend LLM répond au contrat OpenAI-compatible (`/v1/chat/completions`).

## 6. Travaux restants (v3.x)

- Orchestrateur de provisioning multi-instances (allocation de ports
  automatisée via `.registry`).
- Fine-tuning LoRA par métier (v3.1).
