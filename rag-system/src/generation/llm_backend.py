"""
Détection du backend LLM à partir de l'URL (LLM_URL).

Luciole Prime est bi-architecture :
- x86/AMD (Windows 11 + WSL2) : Ollama → hot-swap possible (gestion dynamique UI).
- ARM64 GX10/DGX Spark : TensorRT-LLM → modèle figé au lancement du container.

Seul Ollama est détecté pour la gestion dynamique depuis l'UI. Tout autre
backend OpenAI-compatible (LM Studio, vLLM, etc.) reste utilisable comme moteur
d'inférence via LLM_URL, mais est traité comme ``tensorrt-llm`` (pas de
hot-swap : les routes /api/ollama/* renvoient 501).

Ce module centralise l'heuristique de détection pour que l'API agent, le
proxy UI (feedback_ui) et les tests partagent exactement la même logique.
"""

import os
from typing import Literal

OLLAMA = "ollama"
TENSORRT_LLM = "tensorrt-llm"

Backend = Literal["ollama", "tensorrt-llm"]


def detect_llm_backend(url: str | None = None) -> Backend:
    """
    Déduit le backend LLM depuis l'URL.

    Détection :
      - Ollama : ``ollama`` ou port ``:11434`` → hot-swap possible.
      - Tout le reste (TensorRT-LLM, LM Studio, vLLM, URL inconnue) :
        ``tensorrt-llm`` → pas de hot-swap, aucune route de gestion dynamique
        exposée par erreur.
    """
    if url is None:
        url = os.environ.get("LLM_URL", "")
    u = (url or "").lower()

    if "ollama" in u or ":11434" in u:
        return OLLAMA
    return TENSORRT_LLM


def backend_supports_hot_swap(backend: str) -> bool:
    """True si le backend permet pull/activate/delete de modèles à chaud."""
    return backend == OLLAMA
