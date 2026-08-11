# -*- coding: utf-8 -*-
"""
Agent Profiles - Chargement des profils agentiques métier (YAML)

Reprend le mécanisme déjà en place pour BUSINESS_PROFILE (query rewriter,
voir config/profiles/) : un profil par métier, sélectionné par variable
d'environnement, monté en volume Docker sans rebuild d'image, et
rechargeable à chaud pour l'UI Admin.

Emplacement des profils : config/agent_profiles/<nom>.yaml
Sélection : variable d'environnement AGENT_PROFILE (défaut: "generic")

Un profil déclare :
    name: identifiant du profil
    max_steps: nombre max d'itérations de la boucle agentique
    tools_allowed: liste des tools autorisés (voir tools.py)
    default_metadata_filters: filtres appliqués par défaut aux recherches
        (ex: {"client": "<nom_client>"} pour un index multi-client)
    system_prompt: prompt système du planificateur
    stop_conditions: {min_sources, require_citation}
    routing_rules: règles de routage optionnelles (non exécutées par ce
        module — laissées à l'orchestrateur ou à un appelant, cf. README)
"""

import os
from typing import Dict, Optional

import yaml
from loguru import logger


DEFAULT_PROFILE_NAME = "generic"

# Profil de secours utilisé si aucun fichier YAML n'est trouvable, pour que
# le mode agentique reste utilisable même en environnement mal configuré.
FALLBACK_PROFILE: Dict = {
    "name": "generic",
    "max_steps": 5,
    "tools_allowed": ["search_documents", "search_multi", "get_document", "final_answer"],
    "default_metadata_filters": {},
    "system_prompt": (
        "Tu es Luciole, un assistant documentaire. Utilise les outils de "
        "recherche pour rassembler des informations avant de répondre. "
        "Ne jamais inventer de données. Cite systématiquement tes sources."
    ),
    "stop_conditions": {"min_sources": 1, "require_citation": True},
    "routing_rules": [],
}

REQUIRED_KEYS = ("name", "max_steps", "tools_allowed", "system_prompt")


class AgentProfileError(Exception):
    """Levée quand un profil demandé est invalide ou introuvable."""
    pass


def _candidate_paths(profile_name: str, profiles_dir: Optional[str] = None) -> list:
    """Construit la liste des chemins possibles pour le fichier de profil,
    du plus spécifique (chemin explicite fourni) au plus générique
    (répertoires standards du conteneur)."""
    filename = f"{profile_name}.yaml"
    if profiles_dir:
        return [os.path.join(profiles_dir, filename)]
    return [
        os.path.join("config", "agent_profiles", filename),
        os.path.join("/app", "config", "agent_profiles", filename),
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "config", "agent_profiles", filename),
    ]


def load_profile(profile_name: Optional[str] = None, profiles_dir: Optional[str] = None) -> Dict:
    """
    Charge un profil agentique depuis son fichier YAML.

    Args:
        profile_name: nom du profil (sans extension). Si None, lu depuis
            la variable d'environnement AGENT_PROFILE, avec repli sur
            DEFAULT_PROFILE_NAME.
        profiles_dir: répertoire explicite où chercher le fichier
            (surtout utile pour les tests). Si None, utilise les
            emplacements standards du conteneur.

    Returns:
        dict représentant le profil, avec toutes les clés de
        FALLBACK_PROFILE garanties présentes (complétées si absentes
        du YAML).

    Ne lève jamais d'exception pour un profil manquant ou invalide :
    retombe sur FALLBACK_PROFILE en journalisant un avertissement, pour
    qu'une erreur de configuration n'interrompe jamais le service.
    """
    name = profile_name or os.environ.get("AGENT_PROFILE", DEFAULT_PROFILE_NAME)

    for path in _candidate_paths(name, profiles_dir):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = yaml.safe_load(f) or {}
            except Exception as e:
                logger.warning(f"Erreur lecture profil agentique '{path}': {e}")
                continue

            missing = [k for k in REQUIRED_KEYS if k not in raw]
            if missing:
                logger.warning(
                    f"Profil agentique '{name}' ({path}) incomplet "
                    f"(clés manquantes: {missing}), complété avec les valeurs par défaut"
                )

            merged = dict(FALLBACK_PROFILE)
            merged.update(raw)
            merged["name"] = raw.get("name", name)
            logger.info(f"Profil agentique chargé: '{merged['name']}' depuis {path}")
            return merged

    logger.warning(
        f"Profil agentique '{name}' introuvable (chemins essayés: "
        f"{_candidate_paths(name, profiles_dir)}), utilisation du profil générique de secours"
    )
    return dict(FALLBACK_PROFILE)


def list_available_profiles(profiles_dir: Optional[str] = None) -> list:
    """
    Liste les noms de profils disponibles (fichiers .yaml présents dans
    le répertoire de profils), pour alimenter un sélecteur dans l'UI Admin.
    """
    search_dirs = [profiles_dir] if profiles_dir else [
        os.path.join("config", "agent_profiles"),
        os.path.join("/app", "config", "agent_profiles"),
    ]
    for d in search_dirs:
        if d and os.path.isdir(d):
            names = sorted(
                fname[:-5] for fname in os.listdir(d)
                if fname.endswith(".yaml")
            )
            if names:
                return names
    return [DEFAULT_PROFILE_NAME]


# ============================================================================
# SINGLETON PATTERN (même approche que get_query_rewriter dans
# config/query_rewriter.py) — avec fonction explicite de rechargement à
# chaud pour l'UI Admin.
# ============================================================================

_active_profile: Optional[Dict] = None


def get_active_profile(force_reload: bool = False) -> Dict:
    """
    Retourne le profil agentique actif (singleton), chargé depuis
    AGENT_PROFILE au premier appel.

    Args:
        force_reload: force une relecture du fichier YAML (utilisé par le
            bouton "recharger" de l'UI Admin), sans redémarrer le service.
    """
    global _active_profile
    if _active_profile is None or force_reload:
        _active_profile = load_profile()
    return _active_profile


def reload_active_profile(profile_name: Optional[str] = None) -> Dict:
    """
    Recharge explicitement le profil actif, en changeant éventuellement de
    profil (utilisé par l'UI Admin pour basculer d'instance sans
    redémarrage). Si profile_name est None, recharge le profil courant
    depuis son fichier (utile après une édition manuelle du YAML).
    """
    global _active_profile
    name = profile_name or (_active_profile or {}).get("name")
    _active_profile = load_profile(name)
    return _active_profile
