# -*- coding: utf-8 -*-
"""
LLM Generator — Génération de réponses via API OpenAI-compatible

V5 : TensorRT-LLM 1.2 backend PyTorch (Qwen3-30B-A3B-Instruct-2507 NVFP4, GX10)
     API OpenAI-compatible exposée par Triton Inference Server + TRT-LLM backend.
     Base URL et model lus depuis settings.yaml ou variable d'env LLM_URL.
"""

import os
import yaml
import httpx
from typing import Optional, Dict, Any, List
from loguru import logger

try:
    from src.config_loader import load_prompts
except ImportError:
    try:
        from config_loader import load_prompts
    except ImportError:
        load_prompts = None
        logger.warning("config_loader non disponible, prompts par défaut utilisés")


class LLMGenerator:
    """
    Générateur de réponses LLM via API OpenAI-compatible (v5).
    Backend : TensorRT-LLM 1.2 (Qwen3-30B-A3B-Instruct-2507 NVFP4) via trtllm-serve.
    Compatible aussi : vLLM, Ollama, LM Studio, OpenAI (fallback).
    """

    def __init__(self, config_path: str = None):
        if config_path is None:
            config_path = os.environ.get("CONFIG_PATH", "config/settings.yaml")
        self._load_llm_config(config_path)
        self._load_prompts_config()
        logger.info(
            f"LLMGenerator v5 [TensorRT-LLM]: model={self.model}, base_url={self.base_url}"
        )

    def _load_llm_config(self, config_path: str):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
        except Exception as e:
            logger.warning(f"Erreur chargement {config_path}: {e}")
            config = {}

        llm_config = config.get("llm", {})

        # Priorité : variable d'env > settings.yaml > défaut TensorRT-LLM
        env_url = (
            os.environ.get("LLM_URL")
            or os.environ.get("TRT_LLM_URL")
            or os.environ.get("TRITON_URL")
        )
        yaml_url = llm_config.get("base_url", "http://tensorrt-llm:8000")
        base = env_url if env_url else yaml_url
        self.base_url = base if base.endswith("/v1") else f"{base}/v1"

        # Nom du modèle exposé par le serveur TRT-LLM (= SERVED_MODEL_NAME)
        self.model       = llm_config.get("model",       "qwen3-30b-a3b-instruct")
        self.temperature = llm_config.get("temperature",  0.1)
        self.max_tokens  = llm_config.get("max_tokens",   4096)
        self.timeout     = llm_config.get("timeout",      300)
        # num_ctx non requis par TRT-LLM (context window compilée au build time)
        self.num_ctx     = llm_config.get("num_ctx",      32768)

    def _load_prompts_config(self):
        if load_prompts is not None:
            try:
                self.prompts_config = load_prompts()
                return
            except Exception as e:
                logger.warning(f"Erreur chargement prompts: {e}")
        self.prompts_config = None

    # =========================================================================
    # MÉTHODE PUBLIQUE — appel LLM simple
    # =========================================================================

    def call_llm(self, system_prompt: str, prompt: str) -> str:
        """Appel LLM simple pour résumés, analyses, comparaisons."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": prompt}
        ]
        return self._call_llm(messages)

    # Alias rétrocompatible (déprécié — sera supprimé en v6)
    def _call_ollama(self, system_prompt: str, prompt: str) -> str:
        logger.warning("_call_ollama() est déprécié — utiliser call_llm()")
        return self.call_llm(system_prompt, prompt)

    # =========================================================================
    # GENERATE — appel RAG complet avec historique
    # =========================================================================

    def generate(
        self,
        query: str,
        context: str,
        search_results: list = None,
        custom_prompt: str = None,
        history: list = None
    ) -> Dict[str, Any]:
        system_prompt = self._build_system_prompt(custom_prompt)
        user_prompt   = self._format_rag_prompt(context, query) if context else query

        messages = [{"role": "system", "content": system_prompt}]
        if history:
            for msg in history:
                role, content = msg.get("role", "user"), msg.get("content", "")
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_prompt})

        try:
            response_text = self._call_llm(messages)
        except Exception as e:
            logger.error(f"Erreur génération LLM: {e}")
            response_text = f"Erreur lors de la génération: {e}"

        return {
            "response":   response_text,
            "sources":    self._extract_sources(search_results),
            "confidence": 0.8 if context else 0.3,
            "model":      self.model
        }

    # =========================================================================
    # HEALTH CHECK
    # =========================================================================

    def health_check(self) -> bool:
        """Vérifie que TensorRT-LLM / Triton répond sur /v1/models."""
        try:
            with httpx.Client(timeout=10.0) as client:
                r = client.get(f"{self.base_url}/models")
                return r.status_code == 200
        except Exception as e:
            logger.warning(f"TRT-LLM health check failed: {e}")
            return False

    # =========================================================================
    # MÉTHODE INTERNE — unique point d'appel HTTP
    # =========================================================================

    def _call_llm(self, messages: list) -> str:
        """HTTP POST vers /v1/chat/completions (standard OpenAI / TRT-LLM)."""
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model":       self.model,
            "messages":    messages,
            "stream":      False,
            "temperature": self.temperature,
            "max_tokens":  self.max_tokens,
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                r = client.post(url, json=payload)
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
        except httpx.TimeoutException:
            raise RuntimeError(
                f"TRT-LLM timeout ({self.timeout}s) — vérifiez que le moteur est chargé."
            )
        except httpx.ConnectError:
            raise RuntimeError(
                f"TRT-LLM inaccessible ({self.base_url}) — vérifiez que le service Triton est démarré."
            )
        except Exception as e:
            logger.error(f"Erreur API TRT-LLM: {e}")
            raise

    # =========================================================================
    # UTILITAIRES
    # =========================================================================

    def _build_system_prompt(self, custom_prompt: str = None) -> str:
        if self.prompts_config:
            base = self.prompts_config.get_system_prompt()
        else:
            base = (
                "Tu es Luciole, un assistant documentaire. "
                "Tu t'appuies sur les documents fournis pour répondre. "
                "Ne jamais inventer de données. Cite tes sources."
            )
        return f"{base}\n\n{custom_prompt}" if custom_prompt else base

    def _format_rag_prompt(self, context: str, query: str) -> str:
        return (
            f"Voici des extraits de documents pertinents :\n\n{context}\n\n---\n\n"
            f"Question : {query}\n\n"
            "Réponds en t'appuyant exclusivement sur les extraits ci-dessus. "
            "Si l'information n'est pas présente, dis-le clairement."
        )

    def _extract_sources(self, search_results: list = None) -> list:
        if not search_results:
            return []
        seen, sources = set(), []
        for r in search_results:
            key = r.get("file_path") or r.get("file_name", "")
            if key and key not in seen:
                seen.add(key)
                sources.append({
                    "file_name": r.get("file_name", ""),
                    "file_path": r.get("file_path", ""),
                    "score":     round(r.get("rrf_score", r.get("score", 0)), 4)
                })
        return sources[:10]
