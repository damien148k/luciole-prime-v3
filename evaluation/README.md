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
| `campagne_query2.py` | rejoue un jeu de remarques sur le pipeline iteratif `/api/query2` et compte les esquives, le bon tome et les verdicts de couverture. |
| `campagne_query2_deep.py` | rejoue le meme jeu deux fois sur `/api/query2` — `deep_search` desactive puis active — et croise les verdicts cas par cas (ameliores / degrades), avec controle de plomberie sur le nombre de passages soumis au modele. |
| `exporter_echanges.py` | exporte les echanges de l'interface depuis les bases de journalisation, avec les verdicts, pour relecture. |
| `diag_rappel_pages.py` | extraction des references de tome et de page, utilisee par les campagnes. |

## Champ des références attendues

Les campagnes lisent les références attendues de chaque cas dans le champ
`sources_citees_reference` du jeu de questions. Pour un jeu construit sous
un autre nom de champ, passer `CHAMP_REFERENCE` :

```
docker exec -e CHAMP_REFERENCE=mon_champ \
    luciole-agent-<instance> python /app/evaluation/campagne_consigne.py
```


## Ce qui reste fourni par l'instance

Le jeu de questions. Il porte les remarques d'un dossier reel, il n'a donc
rien a faire dans le depot. Les campagnes le lisent a
`/app/evaluation/jeu_test_mrae.jsonl`, chemin surchargeable :

```
docker exec -e JEU=/app/evaluation/jeu_test_mon_dossier.jsonl \
  luciole-agent-<instance> python /app/evaluation/campagne_consigne.py
```

Format attendu, une ligne par cas :

```json
{"id": "cas-01", "question": "...", "citations": ["Tome 5 p. 202"]}
```

## Le detecteur de verdict

`exporter_echanges.py` porte la definition de reference, dans la fonction
`verdict()`. `campagne_consigne.py` l'importe, `campagne_reproductibilite.py`
en detient encore une copie a resorber. Trois etats :

- `ESQUIVE` : l'absence est annoncee dans les quinze premiers pour cent du
  texte, ou la reponse fait moins de sept cents caracteres, ou elle ne cite
  aucun document. Seul cas compte comme echec.
- `concede` : l'absence est posee apres la substance.
- `repond` : aucune annonce d'absence.

Le motif reconnait les formulations d'absence au singulier comme au
pluriel. Sa premiere version ne couvrait que le pluriel, parce qu'elle
avait ete ecrite en recopiant des reponses ou le sujet etait toujours
"les extraits" : une esquive formulee "le dossier ne mentionne pas"
passait pour une reponse valide.

`campagne_consigne.py` reporte aussi, en colonne parallele, le verdict
d'un detecteur naif qui compte toute mention d'absence comme un echec.
L'ecart entre les deux colonnes est une information : ce detecteur naif
avait donne treize echecs la ou il y en avait deux, en comptant comme
faute la phrase que la consigne prescrit.

## Ce que les campagnes mesurent, et sur quel champ

Le bon tome se calcule sur le champ `passages` de la reponse d'API, qui
porte les trente entrees soumises au modele. Il ne doit pas se calculer
sur `sources`, que `_extract_sources` (`llm.py`) termine par
`return sources[:10]` : une reponse citant correctement un document
classe au-dela du dixieme serait comptee fausse.

La profondeur de recherche vaut 100 passages, imposee par
`LIMITS["standard"]["max_total_chunks"]` (`analyzer.py`). Le champ `top_k`
de la requete ne la regle pas : `api.py` le convertit en
`options["max_items"]`, que `_analyze_chat` ne recoit pas.

Le nombre d'extraits reellement soumis au modele vaut `rerank_top_n`, lu
dans `settings.yaml`. Il etait auparavant plafonne en dur a quinze dans
`_build_context`.

## Repere en vigueur

Beaumont Sud, 30 aout 2026 — premiere mesure a fenetre effective
complete : 32768 tokens confirmes par sonde (api_format: ollama,
Ollama 0.33.2, qwen2.5:14b-instruct-q4_K_M), verdict a trois etats,
jeu de 15 remarques, pipeline avec PR #48 (deep_search) et #50.

- standard : 0 esquive, 5 repond, 10 concede ; 11 COUVERT / 4 PARTIEL,
  4 recherches B ; 23,6 min au total.
- deep : 0 esquive, 6 repond, 9 concede ; 13 COUVERT / 2 PARTIEL,
  2 recherches B ; 27,8 min (+18 %).
- croisement : 3 ameliores (concede -> repond : beaumont-02, -04, -14),
  2 degrades (repond -> concede : beaumont-01, -06). Les ameliores
  gagnent un tome rendu (-02 gagne le tome 4, -14 le tome 1) — gain
  de couverture reel. Les degrades ne perdent aucun tome : le modele
  formule plus prudemment face a 30 passages (bruit accepte), ce que
  le verdict compte concede.
- controle de plomberie : 0 cas avec moins de passages en deep.

Conclusion tiree ce jour : le profil deep est un mode d'exhaustivite
a la demande (net +1 verdict, couverture meilleure, recherche B
divisee par deux), pas un defaut — d'ou l'affichage fusionne de la
PR #49 (standard en tete, pistes deep repliees dessous).

Reserve corpus : le Tome 5 (Paysage et Patrimoine, > 500 Mo) etait
refuse par le watcher (limite WATCHER_MAX_FILE_SIZE_MB) et donc
absent des index pendant cette mesure — les remarques paysageres
ont ete jugees sur sources secondaires (RNT, Tome 1). Apres releve
de la limite et ingestion du tome, une nouvelle mesure s'impose au
moins sur ces remarques ; toute modification du corpus invalide le
repere comme tout changement de fenetre.

L'ancien repere du 6 aout 2026 reste retire : trois defauts cumules,
tous decouverts le 7 aout, le rendaient inexploitable.

1. `num_ctx` n'etait jamais transmis a Ollama. La fenetre effective
   valait 2048 tokens au lieu des 16384 configures. Avec `keep=4`, la
   consigne de generation et les passages les mieux classes etaient tous
   deux ecartes : le modele ne voyait que la fin de la liste d'extraits.
2. `_build_context` plafonnait le contexte a quinze extraits en dur,
   quelle que soit la valeur de `rerank_top_n`.
3. Le detecteur d'esquive ne reconnaissait que les formulations au
   pluriel. Une esquive au singulier etait comptee comme une reponse.

Le 30 aout 2026, la sonde a retrouve le meme defaut sur l'instance mrae
reinstallee : 2048 tokens effectifs malgre `num_ctx: 16384`. Depuis,
`api_format: ollama` (cle qui existait dans settings.yaml sans etre lue)
fait appel a l'API native `/api/chat`, qui transmet `options.num_ctx` a
chaque requete : la fenetre redevient pilotable depuis settings.yaml,
instance par instance, sans toucher au serveur. `sonde_num_ctx_natif.py`
prouve que la version du serveur honore ce pilotage avant de s'y fier ;
`sonde_contexte.py` reste la mesure de la fenetre effective, quel que
soit le chemin d'appel. Toute mesure de reference posterieure a un
changement de fenetre doit etre refaite : les passages visibles par le
modele changent, donc les reponses.
