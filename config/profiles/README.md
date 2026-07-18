# Profils métier — `BUSINESS_PROFILE`

Luciole Prime v3 adopte un positionnement **multi-métier neutre**. Par défaut,
le *query rewriter* (`rag-system/src/retrieval/query_rewriter.py`) ne contient
**aucune règle d'enrichissement métier** (`BUSINESS_RULES = []`).

Les règles spécifiques à un domaine sont désormais externalisées dans ce
dossier, un fichier par métier :

| Profil        | Fichier                              | État                        |
|---------------|--------------------------------------|-----------------------------|
| `generic`     | `query_rewriter.generic.py`          | Vide (défaut)               |
| `eolien`      | `query_rewriter.eolien.py`           | **Complet** (héritage v2)   |
| `horlogerie`  | `query_rewriter.horlogerie.py`       | Template à compléter        |
| `crm`         | `query_rewriter.crm.py`              | Template à compléter        |
| `petrochimie` | `query_rewriter.petrochimie.py`      | Template à compléter        |

## Mécanisme `BUSINESS_PROFILE`

Le choix du profil se fait via la variable d'environnement `BUSINESS_PROFILE` :

```bash
BUSINESS_PROFILE=eolien   # ou horlogerie, crm, petrochimie, generic (défaut)
```

Le profil sélectionné définit la liste `BUSINESS_RULES` chargée par le query
rewriter. Deux façons de l'appliquer :

### 1. Montage volume Docker (recommandé)

Monter le fichier de profil par-dessus le module chargé au runtime, sans
reconstruire l'image :

```yaml
# docker-compose.instance.yml (extrait)
services:
  rag-system:
    environment:
      - BUSINESS_PROFILE=eolien
    volumes:
      - ./config/profiles/query_rewriter.${BUSINESS_PROFILE:-generic}.py:/app/src/retrieval/business_rules.py:ro
```

Le query rewriter charge alors `BUSINESS_RULES` depuis ce module monté si
présent, sinon retombe sur la liste vide interne.

### 2. Copie manuelle (déploiement bare-metal)

```bash
cp config/profiles/query_rewriter.eolien.py \
   rag-system/src/retrieval/business_rules.py
```

## Ajouter / modifier des règles

Chaque règle est un tuple `(pattern_regex, "termes ajoutés", "identifiant")` :

```python
BUSINESS_RULES = [
    (r"\bimpact\s+(sur\s+)?(les?\s+)?avifaune",
     "mortalite collision avifaune rapaces migrateurs nicheurs",
     "impact_avifaune"),
]
```

- Le `pattern` est une regex compilée en `re.IGNORECASE`.
- Les `termes ajoutés` sont **ajoutés** à la requête (la requête d'origine est
  conservée intacte).
- L'`identifiant` sert au logging / débogage.

Voir `query_rewriter.eolien.py` pour un exemple complet (15 règles).

## Synonymes simples

Pour de simples synonymes bidirectionnels (sans enrichissement lourd), utilisez
plutôt `config/synonyms.txt`, rechargeable à chaud via l'UI Admin — indépendant
du profil métier.
