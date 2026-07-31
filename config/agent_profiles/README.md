# Profils agentiques — `AGENT_PROFILE`

Luciole Prime v3 expose un mode agentique (boucle bornée plan/act/observe,
voir `rag-system/src/agent/orchestrator.py`) en complément du mode
procédural existant (`DocumentAnalyzer`). Chaque instance sélectionne son
comportement agentique via un profil YAML, sur le même principe que
`BUSINESS_PROFILE` pour le query rewriter (voir `config/profiles/README.md`).

| Profil            | Fichier                  | État                          |
|-------------------|---------------------------|-------------------------------|
| `generic`         | `generic.yaml`             | Défaut, sans restriction métier |
| `belacom_support`  | `belacom_support.yaml`     | Support technique Belacom (escalade activée) |

## Mécanisme `AGENT_PROFILE`

Le choix du profil se fait via la variable d'environnement `AGENT_PROFILE` :

```bash
AGENT_PROFILE=belacom_support   # ou generic (défaut)
```

Chargement (voir `rag-system/src/agent/agent_profiles.py`) :

1. Lecture de `AGENT_PROFILE` (défaut `generic` si absent)
2. Recherche du fichier `config/agent_profiles/<profil>.yaml` dans les
   emplacements standards du conteneur
3. Fusion avec un profil de secours interne si des clés sont manquantes,
   pour qu'une erreur de configuration n'interrompe jamais le service
4. Si le fichier est introuvable : repli complet sur le profil générique
   de secours, avec avertissement journalisé

### Montage volume Docker (recommandé)

Comme pour les profils métier du query rewriter, monter le fichier de
profil par-dessus le dossier de config, sans reconstruire l'image :

```yaml
# docker-compose.instance.yml (extrait)
services:
  agent:
    environment:
      - AGENT_PROFILE=belacom_support
    volumes:
      - ./config/agent_profiles:/app/config/agent_profiles:ro
```

### Rechargement à chaud

`agent_profiles.get_active_profile()` garde le profil actif en mémoire
(singleton). Le bouton "Recharger" prévu dans l'onglet Admin doit appeler
`agent_profiles.reload_active_profile()`, qui relit le fichier YAML sans
redémarrer le conteneur — même logique que `reload_synonyms()` pour
`synonyms.txt`.

## Structure d'un profil

```yaml
name: mon_profil
max_steps: 5                       # itérations max de la boucle agentique
tools_allowed:                     # voir tools.py pour la liste complète
  - search_documents
  - search_multi
  - get_document
  - escalate_to_human             # optionnel, selon le métier
  - final_answer
default_metadata_filters: {}       # ex: {"client": "belacom"}
system_prompt: |
  Instructions du planificateur agentique...
stop_conditions:
  min_sources: 1                   # nombre min de sources avant d'accepter final_answer
  require_citation: true
routing_rules: []                  # documentaire pour l'instant, voir note ci-dessous
```

`final_answer` reste toujours disponible même si omis de `tools_allowed` —
l'orchestrateur l'ajoute systématiquement comme garde-fou pour que la
boucle puisse toujours se terminer proprement.

## Limite connue : `routing_rules`

Les `routing_rules` déclarées dans les profils sont pour l'instant
**informatives uniquement** — elles documentent l'intention (ex: escalade
automatique sur sévérité critique) mais ne sont pas exécutées par
l'orchestrateur v1, qui laisse le LLM planificateur décider seul en
s'appuyant sur les instructions du `system_prompt`. Une évaluation directe
des métadonnées des résultats de recherche (sans dépendre du jugement du
LLM) est une évolution possible pour une v2, si le taux de bonnes décisions
d'escalade s'avère insuffisant en usage réel.

## Ajouter un nouveau profil métier

1. Copier `generic.yaml` vers `<nom_metier>.yaml`
2. Ajuster `tools_allowed`, `default_metadata_filters`, `system_prompt` et
   `stop_conditions` selon le métier
3. Définir `AGENT_PROFILE=<nom_metier>` pour l'instance concernée
4. Redémarrer le conteneur `agent` (ou recharger à chaud depuis l'UI Admin
   une fois cette fonctionnalité disponible)
