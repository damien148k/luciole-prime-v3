# Redeployer une instance et retrouver les memes reponses

Ce document sert un objectif precis : installer Luciole sur une autre
machine et obtenir, sur le meme corpus, les memes reponses qu'une instance
de reference. Un clone du depot n'y suffit pas, et la raison est ecrite
dans `.gitignore` : la configuration, le corpus, les index, les modeles et
les bases de retours ne sont pas versionnes.

## Ce que le depot porte, et ce qu'il ne porte pas

| dans le depot | hors depot, a transporter |
|---|---|
| tout le code de `rag-system/src` | `.env` de l'instance |
| les installeurs et les composes | `config/settings.yaml` |
| `config/settings.yaml.example` | `config/auth.yaml`, les comptes |
| les profils d'agent d'exemple | le profil d'agent du metier |
| les outils de `evaluation/` | `data/`, le corpus |
| cette documentation | `models/`, les poids |
| | `opensearch_data/`, les index |
| | `feedbacks/`, l'historique des echanges |

## Etape 1, relever l'instance de reference

Sur la machine actuelle :

```
docker exec luciole-agent-<instance> python /app/evaluation/figer_instance.py
docker cp luciole-agent-<instance>:/app/evaluation/paquet_<instance>.zip .
```

Le paquet contient la configuration effective avec les secrets masques, le
profil d'agent reellement resolu, les modeles servis avec leurs parametres,
l'inventaire des index, l'inventaire du corpus avec une empreinte par
document, et l'empreinte git de chaque fichier de code execute. Le releve
ne suppose rien : chaque valeur est lue dans le fichier ou obtenue du
service qui la detient.

## Etape 2, installer sur la machine cible

```
git clone https://github.com/damien148k/luciole-prime-v3
cd luciole-prime-v3
.\INSTALL.ps1 -Profile gpu
```

Docker Desktop doit tourner. L'installeur demande le nom de l'instance et
la cree sous `C:\RAG\luciole-<instance>`.

## Etape 3, restaurer ce qui n'etait pas versionne

Dans le dossier de l'instance creee :

1. `config/settings.yaml` : partir du `settings.yaml` du paquet, pas de
   `settings.yaml.example`. Retablir les valeurs masquees.
2. `config/agent_profiles/<profil>.yaml` : le `profil_*.yaml` du paquet.
3. `.env` : reprendre `INSTANCE_NAME`, `AGENT_PROFILE`, et poser
   `CHAT_ROUTE` a la meme valeur que la reference.
4. `data/inbox` : le corpus, verifie contre `corpus.txt` du paquet.
5. `models/` : retelecharger les modeles listes dans `modeles.json`, en
   verifiant l'empreinte et les parametres du Modelfile, `num_ctx` en
   particulier.
6. `feedbacks/` : facultatif, seulement pour conserver l'historique.

## Etape 4, verifier que le moteur est bien le meme

```
docker exec luciole-agent-<instance> python /app/evaluation/preparer_determinisme.py
```

L'outil compare l'empreinte git de chaque fichier de `src` a la reference,
relit `temperature` et `seed` dans la configuration, et signale tout ecart.
Trois reglages decident de la reproductibilite :

| reglage | valeur | pourquoi |
|---|---|---|
| `llm.temperature` | `0` | sans quoi deux passages identiques divergent |
| `llm.seed` | `42` | fixe l'echantillonnage residuel |
| `retrieval.rerank_top_n` | identique a la reference | plafonne les passages transmis au modele, donc l'invite |

`llm.py` lit `temperature` avec `0.0` pour defaut et `seed` avec `42`, mais
un `settings.yaml` qui porte une autre valeur l'emporte. Les deux lignes
doivent donc etre explicites.

## Etape 5, prouver que les reponses sont les memes

Ingerer le corpus, puis rejouer le meme jeu de questions que la reference :

```
docker exec -e CONSIGNE=1 -e LABEL=cible luciole-agent-<instance> \
  python /app/evaluation/campagne_consigne.py
```

Repere mesure le 6 aout 2026 sur vingt remarques d'un avis d'autorite
environnementale, avec la consigne de generation :

| grandeur | valeur de reference |
|---|---|
| esquives | 0 sur 20 |
| bon tome cite | 9 sur 10 |
| caracteres medians | 2149 |
| duree | environ 14 minutes |

Et le plancher de bruit, deux passages du meme bras sur la meme machine :

```
docker exec -e PASSAGES=2 luciole-agent-<instance> \
  python /app/evaluation/campagne_reproductibilite.py
```

Reference : dix-neuf reponses sur vingt identiques au caractere, zero
verdict change. Un ecart de plus d'un cas ne s'explique pas par le bruit,
il signale une configuration differente. C'est ce chiffre qui rend la
comparaison entre deux machines concluante.

## Etape 6, la chaine a emprunter

La chaine retenue, celle qui a produit les chiffres ci-dessus, est le
pipeline procedural en une passe, avec la consigne de generation de
`config/prompts/`, sans boucle agentique et sans reecriture de requete.
Pour que l'interface de chat l'emprunte, poser dans le `.env` :

```
CHAT_ROUTE=classique
```

La variable doit aussi figurer dans le bloc `environment` du service
`chat`, ce que les composes du depot font depuis cette version. Une
instance plus ancienne, dont le `docker-compose.yml` a ete genere avant,
ne transmettra rien au conteneur tant que la ligne
`- CHAT_ROUTE=${CHAT_ROUTE:-agent}` n'y est pas ajoutee.

## Ce qui n'est pas garanti par cette procedure

Le materiel. Les chiffres ci-dessus ont ete obtenus sur un GPU donne, avec
un modele quantifie donne. Un autre GPU, un autre niveau de quantification
ou une autre fenetre de contexte servie changent les reponses, meme a
temperature nulle et a graine fixe. Le releve consigne ces trois grandeurs
pour que l'ecart soit constate plutot que subi.
