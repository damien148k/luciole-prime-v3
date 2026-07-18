# =============================================================================
# PROFIL MÉTIER : PÉTROCHIMIE / INDUSTRIE PROCÉDÉS (template — à compléter)
# =============================================================================
# Template vide pour un déploiement pétrochimie / raffinage / industrie de
# procédés (HSE, procédés, maintenance, réglementation Seveso...).
#
# Ajoutez vos règles d'enrichissement sous la forme de tuples :
#   (pattern_regex, "termes techniques ajoutes", "identifiant_regle")
#
# Exemple :
#   (r"\b(danger|risque|seveso)\b",
#    "etude dangers analyse risques scenario accidentel ATEX Seveso barriere securite",
#    "etude_dangers_industrie"),
#
# Voir config/profiles/query_rewriter.eolien.py pour un exemple complet et
# config/profiles/README.md pour le mécanisme BUSINESS_PROFILE.
# =============================================================================

BUSINESS_RULES = [
    # TODO: ajouter les règles métier pétrochimie ici.
]
