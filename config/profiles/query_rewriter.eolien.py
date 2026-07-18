# =============================================================================
# PROFIL MÉTIER : ÉOLIEN / ICPE (études d'impact environnemental)
# =============================================================================
# Ces règles sont l'héritage historique de Luciole Prime v2 (contexte éolien /
# ICPE). Elles ne sont PLUS chargées par défaut dans v3 (positionnement
# multi-métier neutre — voir config/profiles/README.md).
#
# Pour réactiver ce profil :
#   export BUSINESS_PROFILE=eolien
# (le mécanisme monte ce fichier comme config/query_rewriter.py au démarrage —
#  voir config/profiles/README.md pour le détail du montage volume Docker).
#
# Ces règles enrichissent les requêtes utilisateur avec des termes techniques
# pour améliorer la pertinence de la recherche. La requête originale est
# conservée intacte, les termes sont AJOUTÉS à la fin.
#
# Vocabulaire applicable à tout projet soumis à autorisation ICPE
# (éolien, photovoltaïque, carrière, industrie, etc.)
#
# Pour ajouter des synonymes simples sans redémarrage :
#   → config/synonyms.txt (rechargeable à chaud via l'UI Admin)
# =============================================================================

BUSINESS_RULES = [
    # --- Biodiversité : reformulations complexes multi-termes ---
    (r"\bimpact\s+(sur\s+)?(les?\s+)?(oiseaux|avifaune|especes?\s+avicoles?)",
     "mortalite collision avifaune rapaces migrateurs nicheurs sensibilite",
     "impact_avifaune"),

    (r"\bimpact\s+(sur\s+)?(les?\s+)?(chiropteres?|chauves?[- ]?souris)",
     "mortalite barotraumatisme chiropteres gites corridors activite ultrasonore bridage",
     "impact_chiropteres"),

    (r"\bimpact\s+(sur\s+)?(les?\s+)?(biodiversite|milieu\s+naturel|faune\s+et\s+flore)",
     "habitats especes protegees ZNIEFF Natura 2000 continuites ecologiques trame verte trame bleue",
     "impact_biodiversite"),

    # --- Paysage : co-visibilité, photomontages ---
    (r"\bimpact\s+(sur\s+)?(les?\s+)?paysage",
     "impact paysager co-visibilite photomontage ZVI perception visuelle monuments historiques sites inscrits SPR",
     "impact_paysage"),

    (r"\b(co[- ]?visibilite|intervisibilite|visibilite)",
     "photomontage ZVI zone influence visuelle monuments historiques sites classes sites inscrits ABF",
     "covisibilite"),

    # --- Acoustique ---
    (r"\b(impact|nuisance|emission)s?\s+(acoustique|sonore|bruit)",
     "emergence acoustique recepteur sensible campagne mesure plan bridage habitation riveraine",
     "impact_acoustique"),

    # --- Mesures ERC ---
    (r"\bmesures?\s+(d[e']?\s*)?(evitement|reduction|compensation|ERC)",
     "mesures ERC eviter reduire compenser accompagnement suivi post-implantation",
     "mesures_erc"),

    (r"\b(eviter|reduire|compenser)\b",
     "mesures ERC evitement reduction compensation sequenceERC",
     "sequence_erc"),

    # --- Suivi post-implantation ---
    (r"\bsuivi\s+(post[- ]?implantation|environnemental|ecologique|mortalite)",
     "monitoring mortalite bridage protocole suivi avifaune chiropteres habitats retour experience",
     "suivi_post"),

    # --- Avis MRAe / procédures ---
    (r"\b(avis|recommandation|observation)\s+(MRAe|MRAE|autorite\s+environnementale|AE)",
     "avis autorite environnementale recommandations insuffisances memoire reponse",
     "avis_mrae"),

    (r"\b(memoire|reponse)\s+(en\s+)?(reponse|MRAe|MRAE|autorite)",
     "memoire reponse avis MRAe recommandations justifications complements",
     "memoire_reponse"),

    # --- Étude de dangers ---
    (r"\b(etude|analyse)\s+(de\s+)?(dangers?|risques?)",
     "etude dangers analyse risques scenario accidentel probabilite gravite effets dominos perimetre danger",
     "etude_dangers"),

    # --- Hydrologie / zones humides ---
    (r"\b(zone|milieu)s?\s+humides?",
     "zone humide inventaire pedologie sondage piezometrique fonctionnalite compensation",
     "zones_humides"),

    (r"\b(hydrologi|bassin\s+versant|cours\s+d.eau|nappe)",
     "hydrologie hydrographie bassin versant nappe phreatique qualite eau SDAGE SAGE captage",
     "hydrologie"),
]
