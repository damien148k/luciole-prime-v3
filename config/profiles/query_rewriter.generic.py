# =============================================================================
# PROFIL MÉTIER : GENERIC (neutre, par défaut)
# =============================================================================
# Profil neutre sans aucune règle d'enrichissement métier. C'est le
# comportement par défaut de Luciole Prime v3 : le query rewriter n'ajoute
# aucun terme technique spécifique à un domaine.
#
# Pour ajouter des synonymes simples sans redémarrage :
#   → config/synonyms.txt (rechargeable à chaud via l'UI Admin)
#
# Voir config/profiles/README.md pour le mécanisme BUSINESS_PROFILE.
# =============================================================================

BUSINESS_RULES = []
