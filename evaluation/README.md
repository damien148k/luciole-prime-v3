# Outils de mesure et de figeage

Ces outils s'executent dans le conteneur agent, ou le dossier
`evaluation/` de l'instance est monte sur `/app/evaluation`. Ils ne sont
pas importes par le produit : aucun ne fait partie du chemin d'execution
d'une reponse.

```
docker exec luciole-agent-<instance> python /app/evaluation/<outil>.py
```

| outil | ce qu'il fait |
|---|---|
| `figer_instance.py` | releve tout ce qui n'est pas versionne et influence une reponse : configuration, profil, modeles servis, index, corpus, empreintes du code. Produit un paquet et un rapport lisible. |
| `preparer_determinisme.py` | compare le code execute a une reference, relit `temperature` et `seed`, et peut les poser avec sauvegarde horodatee. |
| `campagne_consigne.py` | rejoue un jeu de questions sur le pipeline procedural, avec ou sans consigne de generation, et compte les esquives. |
| `campagne_reproductibilite.py` | rejoue deux fois le meme bras et mesure le plancher de bruit, en comparant les reponses au caractere. |
| `exporter_echanges.py` | exporte les echanges de l'interface depuis les bases de journalisation, avec les verdicts, pour relecture. |
| `diag_rappel_pages.py` | extraction des references de tome et de page, utilisee par les campagnes. |

## Ce qui reste fourni par l'instance

Le jeu de questions. Il porte les remarques d'un dossier reel, il n'a donc
rien a faire dans le depot. Les campagnes le lisent a
`/app/evaluation/jeu_test_mrae.jsonl`, chemin surchargeable :

```
docker exec -e JEU=/app/evaluation/jeu_test_beaumont_sud.jsonl \
  luciole-agent-<instance> python /app/evaluation/campagne_consigne.py
```

Format attendu, une ligne par cas :

```json
{"id": "cas-01", "question": "...", "citations": ["Tome 5 p. 202"]}
```

## Le detecteur de verdict

Les campagnes et l'export partagent le meme detecteur a trois etats, de
facon que deux mesures faites a des dates differentes se comparent :

- `ESQUIVE` : l'absence est annoncee dans les quinze premiers pour cent du
  texte, ou la reponse fait moins de sept cents caracteres, ou elle ne cite
  aucun document. Seul cas compte comme echec.
- `concede` : l'absence est posee apres la substance.
- `repond` : aucune annonce d'absence.

Un detecteur plus naif, qui comptait toute mention d'absence comme un
echec, avait donne treize echecs la ou il y en avait deux : il comptait
comme faute la phrase que la consigne prescrit.

## Repere en vigueur

Mesure du 6 aout 2026, vingt remarques d'un avis d'autorite
environnementale, pipeline procedural avec consigne de generation,
`temperature: 0`, `seed: 42` :

| grandeur | valeur |
|---|---|
| esquives | 0 sur 20 |
| bon tome cite | 9 sur 10 |
| caracteres medians | 2149 |
| duree | environ 14 minutes |
| reponses identiques entre deux passages | 19 sur 20 |
