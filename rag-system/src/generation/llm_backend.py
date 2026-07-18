"""
Détection du backend LLM à partir de l'URL (LLM_URL).

Luciole Prime est bi-architecture :
- x86/AMD (Windows 11 + WSL2) : Ollama ou LM Studio → hot-swap possible.
- ARM64 GX10/DGX Spark : TensorRT-LLM → modèle figé au lancement du container.

Ce module centralise l'heuristique de détection pour que l'API agent, le
proxy UI (feedback_ui) et les tests partagent exactement la même logique.
"""

import os

OLLAMA = "ollama"
LM_STUDIO = "lm_studio"
TENSORRT_LLM = "tensorrt-llm"


def detect_llm_backend(url: str | None = None) -> str:
    """
    Déduit le backend LLM depuis l'URL.

    Ordre de détection (le plus spécifique d'abord) :
      1. LM Studio  : ``lmstudio`` ou port ``:1234``
      2. Ollama     : ``ollama`` ou port ``:11434``
      3. TensorRT-LLM : ``tensorrt``/``trt``/``triton`` ou port ``:8000``

    Défaut sûr (URL vide/inconnue) : ``tensorrt-llm`` → pas de hot-swap, donc
    aucune route de gestion dynamique n'est exposée par erreur.
    """
    if url is None:
        url = os.environ.get("LLM_URL", "")
    u = (url or "").lower()

    if "lmstudio" in u or "lm-studio" in u or ":1234" in u:
        return LM_STUDIO
    if "ollama" in u or ":11434" in u:
        return OLLAMA
    if "tensorrt" in u or "trt" in u or "triton" in u or ":8000" in u:
        return TENSORRT_LLM
    return TENSORRT_LLM


def backend_supports_hot_swap(backend: str) -> bool:
    """True si le backend permet pull/activate/delete de modèles à chaud."""
    return backend in (OLLAMA, LM_STUDIO)
