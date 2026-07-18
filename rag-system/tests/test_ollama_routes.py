"""
Tests — gestion dynamique des modèles Ollama.

Couvre :
  - detect_llm_backend() : ollama vs tensorrt-llm (défaut sûr pour tout le reste) ;
  - GET /api/llm/model expose `backend` + `supports_hot_swap` ;
  - les routes /api/ollama/* renvoient 501 quand le backend est TensorRT-LLM ;
  - les routes /api/ollama/* atteignent Ollama quand le backend le supporte
    (httpx mocké via respx — jamais d'appel réseau réel).
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# rag-system sur le path pour `from src.agent import api`
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.generation.llm_backend import (  # noqa: E402
    detect_llm_backend,
    backend_supports_hot_swap,
)
from src.agent import api  # noqa: E402

respx = pytest.importorskip("respx")


OLLAMA_URL = "http://ollama:11434"
TRT_URL = "http://tensorrt-llm-shared:8000"


@pytest.fixture(autouse=True)
def _stub_config(monkeypatch):
    """Évite la lecture disque de settings.yaml : config en mémoire."""
    api._config = {"llm": {"model": "qwen3:8b", "base_url": OLLAMA_URL}}
    yield
    api._config = None


@pytest.fixture
def client():
    return TestClient(api.app)


# --------------------------------------------------------------------------- #
# detect_llm_backend
# --------------------------------------------------------------------------- #
class TestDetectBackend:
    def test_ollama(self):
        assert detect_llm_backend("http://ollama:11434") == "ollama"
        assert detect_llm_backend("http://localhost:11434") == "ollama"

    def test_tensorrt(self):
        assert detect_llm_backend("http://tensorrt-llm-shared:8000") == "tensorrt-llm"
        assert detect_llm_backend("http://trt:8000/v1") == "tensorrt-llm"

    def test_default_is_safe(self):
        # URL vide/inconnue → tensorrt-llm (pas de hot-swap exposé par erreur)
        assert detect_llm_backend("") == "tensorrt-llm"
        assert detect_llm_backend("http://weird-service:9999") == "tensorrt-llm"

    def test_non_ollama_openai_backend_defaults_to_trt(self):
        # LM Studio / vLLM ou tout backend OpenAI-compatible non-Ollama :
        # utilisable comme moteur d'inférence mais pas de gestion dynamique UI.
        assert detect_llm_backend("http://localhost:1234/v1") == "tensorrt-llm"

    def test_env_fallback(self, monkeypatch):
        monkeypatch.setenv("LLM_URL", "http://ollama:11434")
        assert detect_llm_backend() == "ollama"

    def test_supports_hot_swap(self):
        assert backend_supports_hot_swap("ollama") is True
        assert backend_supports_hot_swap("tensorrt-llm") is False


# --------------------------------------------------------------------------- #
# GET /api/llm/model
# --------------------------------------------------------------------------- #
class TestLlmModelEndpoint:
    def test_ollama_reports_hot_swap(self, client, monkeypatch):
        monkeypatch.setenv("LLM_URL", OLLAMA_URL)
        r = client.get("/api/llm/model")
        assert r.status_code == 200
        data = r.json()
        assert data["backend"] == "ollama"
        assert data["supports_hot_swap"] is True

    def test_trt_reports_no_hot_swap(self, client, monkeypatch):
        monkeypatch.setenv("LLM_URL", TRT_URL)
        r = client.get("/api/llm/model")
        assert r.status_code == 200
        data = r.json()
        assert data["backend"] == "tensorrt-llm"
        assert data["supports_hot_swap"] is False


# --------------------------------------------------------------------------- #
# Routes /api/ollama/* — 501 sur TensorRT-LLM
# --------------------------------------------------------------------------- #
class TestGatedOnTrt:
    def test_list_501(self, client, monkeypatch):
        monkeypatch.setenv("LLM_URL", TRT_URL)
        assert client.get("/api/ollama/models").status_code == 501

    def test_pull_501(self, client, monkeypatch):
        monkeypatch.setenv("LLM_URL", TRT_URL)
        assert client.post("/api/ollama/pull", json={"model": "qwen3:8b"}).status_code == 501

    def test_activate_501(self, client, monkeypatch):
        monkeypatch.setenv("LLM_URL", TRT_URL)
        assert client.post("/api/ollama/activate", json={"model": "qwen3:8b"}).status_code == 501

    def test_search_501(self, client, monkeypatch):
        monkeypatch.setenv("LLM_URL", TRT_URL)
        assert client.get("/api/ollama/search", params={"q": "qwen"}).status_code == 501

    def test_delete_501(self, client, monkeypatch):
        monkeypatch.setenv("LLM_URL", TRT_URL)
        assert client.request("DELETE", "/api/ollama/models", json={"model": "x"}).status_code == 501


# --------------------------------------------------------------------------- #
# Routes /api/ollama/* — atteignent Ollama quand le backend le supporte
# --------------------------------------------------------------------------- #
class TestRoutesReachOllama:
    def test_list_calls_ollama(self, client, monkeypatch):
        monkeypatch.setenv("LLM_URL", OLLAMA_URL)
        with respx.mock(base_url=OLLAMA_URL) as mock:
            mock.get("/api/tags").respond(
                json={"models": [{"name": "qwen3:8b", "size": 5_000_000, "modified_at": "", "digest": "abc"}]}
            )
            mock.post("/api/show").respond(json={"details": {}, "model_info": {}})
            r = client.get("/api/ollama/models")
        assert r.status_code == 200
        data = r.json()
        assert data["models"][0]["name"] == "qwen3:8b"
        assert data["active_model"] == "qwen3:8b"

    def test_delete_calls_ollama(self, client, monkeypatch):
        monkeypatch.setenv("LLM_URL", OLLAMA_URL)
        with respx.mock(base_url=OLLAMA_URL) as mock:
            mock.delete("/api/delete").respond(status_code=200, json={})
            # 'other' != modèle actif 'qwen3:8b' → suppression autorisée
            r = client.request("DELETE", "/api/ollama/models", json={"model": "other:latest"})
        assert r.status_code == 200
        assert r.json()["deleted"] == "other:latest"

    def test_delete_active_refused(self, client, monkeypatch):
        monkeypatch.setenv("LLM_URL", OLLAMA_URL)
        # modèle actif → 409 sans toucher Ollama
        r = client.request("DELETE", "/api/ollama/models", json={"model": "qwen3:8b"})
        assert r.status_code == 409
