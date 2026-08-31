# Déploiement d'une instance Luciole — sans retouche manuelle

État de référence : `main` à partir du merge `75fde6c` (31 août 2026).
Pré-requis sur la machine cible : Docker Desktop actif. Aucun réglage de
contexte ou de taille d'ingestion n'est plus nécessaire après l'installation —
les PR #52 (ingestion jusqu'à 9999 Mo), #53 (`api_format: ollama` +
`num_ctx: 32768`) et #54 (`OLLAMA_CONTEXT_LENGTH=32768`) les portent.

## Machine avec internet (installation en ligne)

1. Télécharger le zip de `main` sur GitHub (bouton **Code → Download ZIP**),
   extraire, entrer dans le dossier.
2. Lancer :

   ```powershell
   .\INSTALL.ps1
   ```

   Répondre au nom du client ; l'outil alloue ports et volumes libres.
3. Déposer les données à indexer dans `C:\RAG\luciole-<nom>\data\`
   (le watcher ingère tout seul, y compris les gros documents).
4. Vérifier (une question test dans le chat, puis) :

   ```powershell
   docker exec luciole-ollama-<nom> ollama ps
   ```

   La colonne **CONTEXT** doit afficher **32768** pendant la requête.

## Machine sans internet (package offline)

Sur la machine connectée (celle qui prépare), depuis une copie à jour de
`main` :

1. Construire le package :

   ```powershell
   .\PREPARE_OFFLINE.ps1
   ```

   Le package embarque maintenant la fenêtre complète et l'ingestion large.
2. Copier le dossier `offline_package` sur la machine cible (clé USB, réseau).
3. Sur la cible : `.\INSTALL_OFFLINE.ps1`, répondre au nom du client.
4. Déposer les données dans `data/`, vérifier via `ollama ps` comme ci-dessus.

## Cas d'un proxy d'entreprise (erreur TLS au téléchargement)

Si `ollama pull` échoue avec « certificate signed by unknown authority », le
modèle existe déjà sur une autre instance — le copier au lieu de télécharger :

```powershell
robocopy "C:\RAG\luciole-<existante>\models\ollama" "C:\RAG\luciole-<nouvelle>\models\ollama" /E
robocopy "C:\RAG\luciole-<existante>\models\huggingface" "C:\RAG\luciole-<nouvelle>\models\huggingface" /E
docker restart luciole-ollama-<nouvelle>
docker compose up -d
```

## Vérifications rapides post-install

| Point | Commande | Attendu |
|---|---|---|
| Fenêtre réelle | `docker exec luciole-ollama-<nom> ollama ps` pendant une requête | CONTEXT = 32768 |
| Config native | `Select-String -Path config\settings.yaml -Pattern "api_format"` | `api_format: ollama` |
| Ingestion large | un fichier > 500 Mo dans `data/` | accepté par le watcher |
| Index | `docker exec luciole-qdrant-<nom> curl -s localhost:6333/collections` | collection `<nom>` |

## Ce qui reste manuel (par conception)

- Déposer les données métier dans `data/`
- Mettre à jour une instance existante : `git pull` puis rebuild + `up -d`
  (les instances déjà déployées ne sont jamais modifiées par une mise à jour
  du dépôt — leur compose et settings sont locaux)
