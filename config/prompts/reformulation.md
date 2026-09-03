# Consigne de reformulation des réponses (route /api/query2)

CONSIGNE PROVISOIRE — à réécrire par l'équipe après les essais.

## Comment ce fichier est utilisé

Quand `reformulation.enabled: true` dans settings.yaml (ou
`reformuler: true` dans le corps de la requête), la réponse générée par
le pipeline est d'abord débarrassée de ses marqueurs de calcul
(`{{calc: ...}}`, évalués par Python), puis réécrite par un second appel
LLM piloté par la consigne ci-dessous.

Seule la partie située APRÈS la ligne `---` est envoyée au modèle : tout
ce qui précède reste de la documentation. Le fichier est relu à chaque
requête — éditez-le, puis appelez `/api/reload-config` : nul besoin de
redémarrer ni de reconstruire.

Un cadre de fidélité est codé en dur côté serveur
(`src/generation/posttraitement.py`, prompt système) et prime sur cette
consigne : chiffres, dates, noms propres, références réglementaires et
citations « document, p. N » doivent être conservés tels quels ; aucun
fait ne peut être ajouté. Inutile de répéter ces règles ici — la
consigne ne décrit que le STYLE attendu.

En cas de défaillance (appel impossible, réponse vide, réponse suspecte
de troncature), la réponse d'origine est renvoyée à l'utilisateur. La
clé `reformulation` de la réponse API trace ce qui s'est passé, et
`response_brute` permet de comparer avant/après.

---

Reformule le texte fourni comme une section de mémoire en réponse du
porteur de projet à l'autorité environnementale.

Style attendu :

- Troisième personne : « Le porteur de projet », jamais « je » ni
  « nous ».
- Ton administratif mesuré et soutenu, sans emphase ni familiarité.
- Prose en paragraphes liés — pas de listes à puces, pas de titres.
- Construction en trois temps : rappel fidèle du point soulevé par
  l'autorité, puis exposition de ce que le dossier apporte sur le point
  (avec ses citations de sources), puis conclusion ferme sur les
  engagements du porteur de projet — jamais de renvoi à une hypothétique
  étude complémentaire quand le dossier répond déjà.
- Les valeurs chiffrées et leurs unités sont reprises exactement, avec
  leur citation de source.
