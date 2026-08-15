# Re-ingérer un PDF après correction de la conversion (PR #38)

Procédure valable pour tout PDF dont la conversion était incomplète.
Cas de référence : volet paysager et patrimonial de l'étude d'impact
« Panière du Fort », dont pymupdf4llm ne rendait que ~35 % du texte
(69/85 pages réduites à leurs titres) avant les correctifs de la PR #38.

Prérequis : PR #38 mergée sur `main`. La purge du fichier supprimé du
dossier surveillé est indépendante des correctifs (c'est une opération
d'index) : si le fichier a été retiré alors que le watcher tournait, la
purge est probablement déjà faite — l'étape 4 le vérifie.

## Étape 1 — Mettre à jour le code

Sur la machine qui héberge l'instance, dans le dépôt cloné :

```powershell
git pull
```

## Étape 2 — Reconstruire l'image

Tous les services d'instance (agent, watcher, chat, admin, feedback)
partagent la même image. Le rebuild est **indispensable** : les
correctifs ajoutent deux dépendances dans l'image (`pymupdf-layout`
via pip, `tesseract-ocr` via apt) — un simple `git pull` + redémarrage
ne suffit pas.

```powershell
# GPU (x86_64)
docker build -f Dockerfile.gpu -t luciole-gpu:latest .

# CPU uniquement
docker build -f Dockerfile.cpu -t luciole-cpu:latest .

# GX10 (ARM64)
docker build -f Dockerfile.gpu.arm64 -t luciole-gpu:latest .
```

Site hors ligne : relancer `PREPARE_OFFLINE.ps1` (la wheel
`pymupdf-layout` est récupérée automatiquement, elle existe en
manylinux x86_64 et aarch64), puis builder avec `Dockerfile.*.offline`.

## Étape 3 — Recréer les conteneurs

Dans le dossier de l'instance (`C:\RAG\luciole-<instance>` sous
Windows, dossier équivalent sous Linux) :

```powershell
docker compose --profile gpu up -d --force-recreate
```

(adapter `--profile cpu` selon l'instance, et ajouter
`-f docker-compose.cpu.override.yml` si utilisé)

Vérifier que les correctifs sont bien embarqués :

```powershell
docker exec luciole-watcher-<instance> pip show pymupdf-layout
docker exec luciole-watcher-<instance> python -c "import pymupdf; print(pymupdf.get_tessdata())"
```

Le premier doit afficher la version 1.28.x ; le second doit renvoyer un
chemin tessdata (preuve que tesseract est détecté).

## Étape 4 — Vérifier la purge de l'ancien index

Le fichier doit être **absent** du dossier surveillé
(`data/inbox/...`) depuis assez longtemps pour que le watcher ait
traité la suppression (polling 5 s + debounce 3 s, quelques minutes
suffisent). Vérifier dans les logs :

```powershell
docker logs luciole-watcher-<instance> --since 2h | Select-String "paysager"
```

et côté Qdrant (aucun point ne doit plus porter le document) :

```powershell
curl -s -X POST http://localhost:6333/collections/<instance>/points/count `
  -H "Content-Type: application/json" `
  -d '{"filter": {"must": [{"key": "document_id", "match": {"value": "5 - PE de la Panière du Fort - Volet paysager et patrimonial.pdf"}}]}}'
```

`count` doit valoir 0. Si des points subsistent, attendre la passe de
réconciliation du watcher (60 s) ou supprimer manuellement par filtre
sur `document_id`.

## Étape 5 — Re-déposer le fichier

Remettre le PDF dans le dossier surveillé. Suivre l'ingestion :

```powershell
docker logs -f luciole-watcher-<instance>
```

Repères attendus dans les logs :

- `Using Tesseract for OCR processing.` — le pipeline layout OCRise le
  texte incrusté dans les cartes et photomontages (normal)
- `Extraction pymupdf4llm: N/169 pages traitées` — progression
- idéalement **aucun** warning `pages re-extraites en texte simple` :
  avec pymupdf-layout, le garde-fou de rendement ne devrait pas
  déclencher. S'il déclenche sur quelques pages, il fait son travail
  (repli texte simple pour ces pages) — le signaler quand même.

Durée indicative : **10 à 20 minutes** pour les 169 pages du volet
paysager (layout + OCR Tesseract, CPU) contre ~1 minute avant. C'est
le prix de la couverture complète ; `markdown_timeout: 0` dans
`settings.yaml` évite tout timeout prématuré.

## Étape 6 — Validation métier

Rejouer la question qui avait mis le défaut en évidence :

> « Quels sites et monuments sont concernés par l'implantation
> d'éoliennes ? »

Critères d'acceptation (extraits du volet paysager, absents de l'index
avant correction) :

- église Saint-Martin de Nouvion-le-Comte, classée MH 1922, 2,5–2,6 km
  de la ZIP (aire d'étude immédiate)
- La Fère à 3,9 km : église Saint-Montain classée, château, Quartier
  Drouot inscrit, 3 rue Henri-Martin
- Moulin de Sénercy (inscrit 1994), église de Nouvion-et-Catillon
- Place Carnégie à Tergnier (inscrite 1998) — section située p. 90,
  donc dans la partie 2 du document
- distinction entre aire immédiate, aire rapprochée et aire élargie

Si la réponse reste incomplète alors que l'index contient ces passages
(vérifiable par une recherche directe dans Qdrant/OpenSearch), le
problème est alors côté retrieval/reranking, pas côté ingestion.

## Configuration optionnelle

Le garde-fou de rendement est actif par défaut (`min_yield_ratio`
0,5 dans le code). Pour le rendre explicite ou l'ajuster, dans le
`config/settings.yaml` de l'instance :

```yaml
pdf:
  min_yield_ratio: 0.5   # 0 = désactivé
```

En cas de comportement inattendu après mise à jour, `min_yield_ratio: 0`
désactive le garde-fou sans toucher au reste (pymupdf-layout reste actif
tant que la wheel est dans l'image).
