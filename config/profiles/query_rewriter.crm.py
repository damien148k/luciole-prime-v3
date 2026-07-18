# =============================================================================
# PROFIL MÉTIER : CRM / RELATION CLIENT (template — à compléter)
# =============================================================================
# Template vide pour un déploiement orienté CRM / support / relation client
# (tickets, comptes, contrats, base de connaissance commerciale...).
#
# Ajoutez vos règles d'enrichissement sous la forme de tuples :
#   (pattern_regex, "termes techniques ajoutes", "identifiant_regle")
#
# Exemple :
#   (r"\b(ticket|incident|reclamation)\b",
#    "ticket incident reclamation SLA escalade priorite resolution",
#    "ticket_support"),
#
# Voir config/profiles/query_rewriter.eolien.py pour un exemple complet et
# config/profiles/README.md pour le mécanisme BUSINESS_PROFILE.
# =============================================================================

BUSINESS_RULES = [
    # TODO: ajouter les règles métier CRM ici.
]
