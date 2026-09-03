# Reformulation des réponses et calculs déterministes (route /api/query2)

Deux mécanismes de post-traitement appliqués APRÈS la génération, sur la
route `/api/query2` uniquement :

1. **Calculs déterministes** — le modèle n'additionne, ne divise et ne
   pourcente jamais lui-même. Quand la réponse exige un calcul, il écrit
   un marqueur `{{calc: expression}}` à la place du résultat ; un
   évaluateur Python (arbre syntaxique, liste blanche d'opérateurs,
   `eval()` inexistant dans ce chemin) calcule et insère le résultat au
   format français (« 12 400 », « 3,2 »). Motivation : une erreur
   d'arithmétique dans un mémoire en réponse à l'autorité
   environnementale est inacceptable, et les LLM en produisent
   régulièrement.
2. **Reformulation** — un second appel LLM réécrit la réponse résolue
   selon une consigne de style stockée dans un fichier
   (`config/prompts/reformulation.md`), relu à chaque requête et donc
   éditable à chaud.

Propriété de sécurité, alignée sur la philosophie du pipeline itératif :
toute défaillance (appel LLM en échec, sortie vide, sortie suspecte de
troncature, consigne absente) replie sur la réponse d'origine. Le
post-traitement ne peut pas rendre la réponse pire que sans lui.

## Activation

Dans `config/settings.yaml` de l'instance :

```yaml
reformulation:
  enabled: true
```

puis un reload-config (UI feedback, ou `POST /api/reload-config`) — sans
rebuild ni redémarrage. Le défaut est `false` : une instance existante
non configurée est strictement inchangée.

Pour un essai ponctuel sans toucher la configuration, le paramètre de
requête `reformuler` l'emporte sur le réglage d'instance :

```bash
curl -X POST http://localhost:8500/api/query2 \
  -H "Content-Type: application/json" \
  -d '{"query": "Quelle part de la surface communale représente l'\''emprise du projet ?", "reformuler": true}'
```

Réglages de la section `reformulation` (tous optionnels) :

| clé | défaut | effet |
|---|---|---|
| `enabled` | `false` | active le post-traitement |
| `fichier_prompt` | `config/prompts/reformulation.md` | consigne de style, relue à chaque requête |
| `recriture` | `true` | `false` = calculs résolus mais pas de second appel LLM |
| `decimales` | `1` | arrondi des résultats `{{calc}}` non entiers |
| `longueur_min_ratio` | `0.3` | garde anti-troncature de la reformulation |

## Écrire la consigne de reformulation

Le fichier livré (`config/prompts/reformulation.md`) porte une consigne
**provisoire** (style mémoire en réponse du porteur de projet) destinée
à être réécrite par l'équipe après les essais. Convention : seule la
partie située après la première ligne `---` est envoyée au modèle ;
l'en-tête reste de la documentation.

Un cadre de fidélité est codé en dur côté serveur (prompt système du
second appel) et prime sur la consigne : chiffres, dates, noms propres,
références réglementaires et citations « document, p. N » sont conservés
tels quels, aucun fait ne peut être ajouté, aucune réserve supprimée.
La consigne ne décrit donc que le style.

## Écrire les calculs : ce que le modèle produit

La consigne `calcul_consigne` de `prompts.yaml` est injectée dans le
prompt système de la génération quand la fonctionnalité est active. Elle
impose au modèle la syntaxe :

```
l'emprise représente {{calc: 12.4 / 388 * 100}} % de la surface communale
```

- Nombres avec **point décimal**, sans séparateur de milliers.
- Opérateurs `+ - * /`, parenthèses.
- Fonctions : `arrondi(x, n)`, `round(x, n)`, `min`, `max`, `abs`,
  `pourcentage(partie, total)` (alias `pct`).
- Chaque valeur d'un marqueur doit provenir d'un extrait cité.

Filets de sécurité côté évaluateur : la notation française (« 12 400,5 »,
espaces fines comprises) est normalisée hors appel de fonction ;
division par zéro, exposants géants et toute construction Python sont
rejetés. Un marqueur en échec reste **visible** dans le texte et tracé
en erreur — jamais silencieusement supprimé.

## Lire la réponse API

Quand le post-traitement a tourné, la réponse porte deux clés
supplémentaires :

- `response` : le texte final (calculs résolus, reformulé si la
  consigne est en place) ;
- `response_brute` : la réponse telle que générée, avant tout
  post-traitement — comparaison A/B en campagne ;
- `reformulation` : la trace — chaque calcul (expression, résultat,
  statut) et le détail de la réécriture (`effectuee`, `motif` de repli
  le cas échéant : `consigne_absente`, `erreur_appel`, `vide`,
  `tronquee`, `desactivee`).

## Limites honnêtes

- **Un appel LLM de plus** par réponse quand la réécriture est active :
  latence quasi doublée sur la phase de génération. `recriture: false`
  supprime ce coût en ne gardant que les calculs.
- **Le marqueur dépend du modèle** : la consigne est généralement
  suivie par Qwen, mais seule une campagne sur votre jeu de questions le
  mesure. La trace `reformulation.calculs` est l'indicateur : si les
  réponses chiffrées n'y produisent aucune entrée, le modèle calcule
  encore lui-même.
- La réécriture n'a pas de vérification sémantique : le cadre système
  interdit d'ajouter des faits, mais le contrôle reste humain — d'où
  `response_brute` exposé pour la revue.

## Tests

```bash
pytest rag-system/tests/agent/test_posttraitement.py
```

18 verrous : arithmétique et notation française, rejets de sécurité de
l'évaluateur, résolution et visibilité des marqueurs en échec, replis de
la reformulation (consigne absente, appel en échec, sortie vide ou
tronquée).
