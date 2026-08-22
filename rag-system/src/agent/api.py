"""
Agent Luciole API - Analyse Avancée de Documents
FastAPI service pour l'agent d'analyse intelligente
"""

import os
import re
import time
import json
import sqlite3
from typing import Dict, List, Optional
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from loguru import logger
import yaml
import httpx

from src.agent.iterative import IterativePipeline

from src.generation.llm_backend import (
    detect_llm_backend,
    backend_supports_hot_swap,
)

# Configuration logging
logger.add(
    "logs/agent.log",
    rotation="10 MB",
    retention="7 days",
    level="DEBUG"
)

app = FastAPI(
    title="Luciole Agent - Analyse Avancée",
    description="Agent intelligent d'analyse documentaire",
    version="1.0.0"
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================================
# Pydantic Models
# ============================================================================

class ScopeModel(BaseModel):
    paths: List[str] = Field(default_factory=list, description="Chemins à analyser")
    file_types: List[str] = Field(default_factory=list, description="Types de fichiers")
    date_range: Optional[Dict] = None
    metadata_filters: Optional[Dict] = Field(
        default=None,
        description=(
            "Filtres sur les champs YAML front matter promus en racine "
            "(client, editor, technology, product, version, support_type, "
            "severity, ticket_id, projet, phase, thematique, departement). "
            "Format: {\"editor\": \"fortinet\"} ou {\"severity\": [\"high\", \"medium\"]}."
        )
    )


class OptionsModel(BaseModel):
    detail_level: str = Field(default="standard", description="quick|standard|deep")
    max_items: int = Field(default=20, description="Nombre max de résultats")
    include_sources: bool = Field(default=True)


class AnalyzeRequest(BaseModel):
    query: str = Field(..., description="Requête utilisateur")
    mode: str = Field(default="auto", description="auto|files|folder|cross|chat")
    scope: Optional[ScopeModel] = None
    options: Optional[OptionsModel] = None
    index_name: Optional[str] = Field(default=None, description="Nom de l'index à interroger")


class SummarizeFileRequest(BaseModel):
    file_path: str = Field(..., description="Chemin du fichier à résumer")
    options: Optional[OptionsModel] = None


class CrossAnalyzeRequest(BaseModel):
    query: str = Field(..., description="Question de comparaison")
    scope: ScopeModel
    options: Optional[OptionsModel] = None


class ChatMessage(BaseModel):
    role: str = Field(..., description="Role: 'user' ou 'assistant'")
    content: str = Field(..., description="Contenu du message")


class QueryRequest(BaseModel):
    query: str = Field(..., description="Question utilisateur")
    top_k: int = Field(default=20, description="Nombre de sources")
    index_name: Optional[str] = Field(default=None, description="Nom de l'index à interroger (Qdrant collection)")
    custom_prompt: Optional[str] = Field(default=None, description="Prompt personnalisé optionnel")
    deep_search: bool = Field(default=False, description="Recherche approfondie (double recherche avec/sans historique)")
    history: List[ChatMessage] = Field(default=[], description="Historique de conversation")




# ============================================================================
# Global instances (lazy loading)
# ============================================================================

_analyzers = {}            # Cache par index_name
_analyzers_ts = {}         # Timestamps de création (TTL)
_ANALYZER_TTL = int(os.environ.get("ANALYZER_CACHE_TTL", "300"))  # 5 min par défaut
_embedder = None
_llm_generator = None
_reranker = None
_config = None

# ============================================================================
# Index unique par instance (règle: 1 instance = 1 métier = 1 index)
# Si INSTANCE_NAME est défini dans l'environnement, l'agent ignore tout
# paramètre `index_name` reçu et force systématiquement cette valeur.
# ============================================================================
INSTANCE_NAME = os.environ.get("INSTANCE_NAME", "")
MULTI_INDEX_MODE = os.environ.get("MULTI_INDEX_MODE", "false").lower() == "true"
if INSTANCE_NAME:
    if MULTI_INDEX_MODE:
        logger.info(f"🎯 INSTANCE_NAME défini : '{INSTANCE_NAME}' — multi-index activé (MULTI_INDEX_MODE=true)")
    else:
        logger.info(f"🎯 INSTANCE_NAME défini : '{INSTANCE_NAME}' — index forcé pour toutes les requêtes")


def _resolve_index_name(requested: Optional[str]) -> str:
    """
    Résout l'index_name effectif selon la règle '1 instance = 1 index'.

    - Si INSTANCE_NAME est défini ET MULTI_INDEX_MODE=false, on force toujours
      INSTANCE_NAME (le paramètre reçu de l'UI est ignoré).
    - Si MULTI_INDEX_MODE=true, on utilise le paramètre reçu (ou INSTANCE_NAME
      en fallback si aucun index n'est spécifié).
    - Sinon, on garde le comportement historique (param reçu, ou fallback config).
    """
    if INSTANCE_NAME and not MULTI_INDEX_MODE:
        if requested and requested != INSTANCE_NAME:
            logger.debug(
                f"index_name '{requested}' reçu mais ignoré — "
                f"force '{INSTANCE_NAME}' (INSTANCE_NAME)"
            )
        return INSTANCE_NAME
    if requested:
        return requested
    if INSTANCE_NAME:
        return INSTANCE_NAME
    return _get_config()["qdrant"]["collection_name"]


def _get_config():
    """Charge la config une seule fois avec override par variables d'environnement"""
    global _config
    if _config is None:
        config_path = os.environ.get("CONFIG_PATH", "config/settings.yaml")
        with open(config_path, "r", encoding="utf-8") as f:
            _config = yaml.safe_load(f)

        if _config is None:
            raise RuntimeError(
                f"Le fichier de configuration {config_path} est vide ou invalide. "
                "Vérifiez qu'il contient bien les sections requises (qdrant, opensearch, llm, embedding, retrieval, reranker)."
            )

        # Override avec variables d'environnement pour Docker
        qdrant_url = os.environ.get("QDRANT_URL")
        if qdrant_url:
            # Parse URL: http://luciole-qdrant:6333 -> host=luciole-qdrant, port=6333
            from urllib.parse import urlparse
            parsed = urlparse(qdrant_url)
            _config.setdefault("qdrant", {})
            _config["qdrant"]["host"] = parsed.hostname or "localhost"
            _config["qdrant"]["port"] = parsed.port or 6333
            logger.info(f"Qdrant config from env: {_config['qdrant']['host']}:{_config['qdrant']['port']}")
        
        opensearch_url = os.environ.get("OPENSEARCH_URL")
        if opensearch_url:
            from urllib.parse import urlparse
            parsed = urlparse(opensearch_url)
            _config.setdefault("opensearch", {})
            _config["opensearch"]["host"] = parsed.hostname or "localhost"
            _config["opensearch"]["port"] = parsed.port or 9200
            logger.info(f"OpenSearch config from env: {_config['opensearch']['host']}:{_config['opensearch']['port']}")
        
        llm_url = os.environ.get("LLM_URL") or os.environ.get("OLLAMA_HOST")
        if llm_url:
            _config.setdefault("llm", {})
            _config["llm"]["base_url"] = llm_url
            logger.info(f"LLM URL config from env: {llm_url}")
    
    return _config


def _get_embedder():
    """Charge l'embedder une seule fois"""
    global _embedder
    if _embedder is None:
        config = _get_config()
        from src.ingestion.embedder import Embedder
        _embedder = Embedder(
            model_name=config["embedding"]["model"],
            device=config["embedding"]["device"]
        )
    return _embedder


def _env_flag(name: str, default: bool) -> bool:
    """Lit un booleen depuis l'environnement, avec repli sur la config.

    La variable d'environnement, quand elle est posee, l'emporte sur la
    valeur issue de settings.yaml : cela permet de basculer un reglage
    pour une campagne de mesure (docker compose run -e ...) sans editer
    la configuration de l'instance. Valeurs acceptees, insensibles a la
    casse : true/1/yes/on et false/0/no/off.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in ("true", "1", "yes", "on"):
        return True
    if value in ("false", "0", "no", "off"):
        return False
    logger.warning(
        f"{name}='{raw}' non reconnu (attendu true/false), "
        f"valeur par defaut conservee: {default}"
    )
    return default


def _get_reranker():
    """Charge le reranker une seule fois.

    Comportement strict par defaut : si le modele ne charge pas,
    l'exception remonte et fait echouer toute route qui en depend
    (chat classique ET agent), au lieu de degrader silencieusement
    vers une recherche non reranked. C'est voulu : un mode degrade
    invisible fausserait toute evaluation de qualite (impossible de
    distinguer un probleme de prompt/pipeline d'une simple absence de
    reranking).

    Echappatoire explicite : positionner RERANKER_OPTIONAL=true dans
    l'environnement pour retrouver l'ancien comportement (log un
    warning, renvoie None, l'appelant tourne sans reranker). A
    n'utiliser qu'en connaissance de cause (ex: environnement de repli
    sans GPU dispo), jamais silencieusement en test de qualite.
    """
    global _reranker
    if _reranker is None:
        config = _get_config()
        from src.retrieval.reranker import Reranker
        optional = os.environ.get("RERANKER_OPTIONAL", "false").lower() == "true"
        try:
            _reranker = Reranker(
                model_name=config["reranker"]["model"],
                device=config["reranker"]["device"],
                top_n=config["retrieval"].get("rerank_top_n", 10)
            )
            logger.info("Reranker charge et actif")
        except Exception as e:
            if optional:
                logger.warning(f"Reranker not available (mode degrade autorise via RERANKER_OPTIONAL=true): {e}")
            else:
                logger.error(f"Reranker indisponible et RERANKER_OPTIONAL n'est pas active: {e}")
                raise RuntimeError(
                    f"Reranker indisponible ({e}). Pour tester/tourner sans reranker "
                    "(mode degrade explicite), positionner RERANKER_OPTIONAL=true."
                ) from e
    return _reranker


def _get_llm_generator():
    """Charge le LLM generator une seule fois"""
    global _llm_generator
    if _llm_generator is None:
        config_path = os.environ.get("CONFIG_PATH", "config/settings.yaml")
        from src.generation.llm import LLMGenerator
        _llm_generator = LLMGenerator(config_path=config_path)
    return _llm_generator




def get_analyzer(index_name: str = None):
    """
    Lazy loading de l'analyseur avec support multi-index
    
    Args:
        index_name: Nom de l'index/collection à utiliser (optionnel)
                   Si None, utilise l'index par défaut de la config
    """
    global _analyzers
    
    config = _get_config()
    
    # Déterminer l'index à utiliser (force INSTANCE_NAME si défini)
    index_name = _resolve_index_name(index_name)
    
    # Vérifier le cache
    if index_name in _analyzers:
        logger.debug(f"Using cached analyzer for index: {index_name}")
        # Vérifier le TTL (invalidation automatique)
        ts = _analyzers_ts.get(index_name, 0)
        if time.time() - ts < _ANALYZER_TTL:
            return _analyzers[index_name]
        else:
            logger.info(f"Cache analyzer expiré pour {index_name} (TTL={_ANALYZER_TTL}s) — reconstruction")
            del _analyzers[index_name]
            del _analyzers_ts[index_name]
    
    logger.info(f"Initializing DocumentAnalyzer for index: {index_name}...")
    
    # Imports
    from src.retrieval.bm25_search import BM25Search
    from src.retrieval.dense_search import DenseSearch
    from src.retrieval.hybrid import HybridSearch
    from src.agent.analyzer import DocumentAnalyzer
    
    # Obtenir l'embedder partagé
    embedder = _get_embedder()
    
    # OpenSearch index = version lowercase de l'index Qdrant
    opensearch_index = index_name.lower()
    
    # Dense search pour cet index
    dense_search = DenseSearch(
        host=config["qdrant"]["host"],
        port=config["qdrant"]["port"],
        collection_name=index_name,
        embedder=embedder
    )
    
    # BM25 search pour cet index
    bm25_search = BM25Search(
        host=config["opensearch"]["host"],
        port=config["opensearch"]["port"],
        index_name=opensearch_index
    )
    
    # Hybrid search - avec paramètres de retrieval du settings.yaml
    hybrid_search = HybridSearch(
        bm25_search=bm25_search,
        dense_search=dense_search,
        bm25_weight=config["retrieval"].get("bm25_weight", 0.5),
        dense_weight=config["retrieval"].get("dense_weight", 0.5),
        bm25_top_k=config["retrieval"].get("bm25_top_k", 100),
        dense_top_k=config["retrieval"].get("dense_top_k", 100)
    )
    
    # Document Analyzer — avec paramètres retrieval depuis settings.yaml
    analyzer = DocumentAnalyzer(
        hybrid_search=hybrid_search,
        llm_generator=_get_llm_generator(),
        reranker=_get_reranker(),
        cache_enabled=True,
        retrieval_config=config.get("retrieval", {})
    )
    
    # Mettre en cache
    _analyzers[index_name] = analyzer
    _analyzers_ts[index_name] = time.time()
    
    logger.info(f"DocumentAnalyzer ready for index: {index_name}")
    
    return analyzer


# ============================================================================
# Query History Logger (for RAGAS evaluation)
# ============================================================================

_QUERY_HISTORY_DB = os.environ.get("QUERY_HISTORY_DB", "/app/feedbacks/ragas.db")


def _init_query_history_db():
    Path(_QUERY_HISTORY_DB).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(_QUERY_HISTORY_DB) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS query_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            question TEXT NOT NULL,
            answer TEXT,
            contexts TEXT,
            index_name TEXT,
            processing_time_ms INTEGER
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS ragas_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, index_name TEXT, question TEXT,
            faithfulness REAL, answer_relevancy REAL, context_recall REAL
        )""")


try:
    _init_query_history_db()
except Exception as e:
    logger.warning(f"Could not init query history DB: {e}")


def _log_query(question: str, answer: str, sources: list, index_name: str,
               processing_time_ms: int = 0, search_results: list = None):
    """Store query+answer+contexts for later RAGAS evaluation."""
    try:
        contexts_texts = []
        for src in (search_results or sources or []):
            if isinstance(src, dict):
                text = src.get("text", "") or src.get("text_with_context", "") or src.get("content", "") or src.get("snippet", "")
                if text:
                    contexts_texts.append(text[:2000])
        with sqlite3.connect(_QUERY_HISTORY_DB) as conn:
            conn.execute(
                "INSERT INTO query_history (timestamp, question, answer, contexts, index_name, processing_time_ms) VALUES (?,?,?,?,?,?)",
                (datetime.now().isoformat(), question, answer,
                 json.dumps(contexts_texts, ensure_ascii=False), index_name, processing_time_ms)
            )
    except Exception as e:
        logger.warning(f"Failed to log query for RAGAS: {e}")




# ============================================================================
# API Endpoints
# ============================================================================

def _reporter_pages(passage: dict, meta: dict) -> None:
    """Reporte les numeros de page des metadonnees vers le passage expose.

    Les fragments PDF portent `page_start` et `page_end` depuis la PR #23,
    poses par le chunker a partir de la table de pagination du parser. La
    projection ne recopiait que `page`, une clef heritee que les PDF ne
    produisent pas : les numeros restaient dans Qdrant sans jamais
    remonter jusqu'a l'appelant. Mesure sur les vingt cas du jeu MRAe
    avant correction : 0 passage renseigne sur 292.

    `page` reste recopiee telle quelle pour les formats qui la posent, et
    est deduite de `page_start` sinon, afin que les affichages existants
    continuent de fonctionner sans changement.
    """
    debut = meta.get("page_start")
    fin = meta.get("page_end")

    if debut is not None:
        passage["page_start"] = debut
    if fin is not None:
        passage["page_end"] = fin

    if meta.get("page"):
        passage["page"] = meta["page"]
    elif debut is not None:
        # Un fragment qui chevauche deux pages est annonce par sa premiere :
        # c'est la page ou commence le texte cite.
        passage["page"] = debut


@app.get("/")
async def root():
    """Health check"""
    return {
        "service": "Luciole Agent",
        "status": "running",
        "version": "1.0.0"
    }


@app.post("/api/reload-config")
async def reload_config():
    """
    Recharge la configuration à chaud sans redémarrer le container.
    Réinitialise les singletons : config, LLM et analyzers.
    Les modèles GPU (embedder, reranker) ne sont PAS rechargés (trop coûteux).
    """
    global _config, _llm_generator, _analyzers
    
    try:
        # 1. Reset settings.yaml cache
        _config = None
        new_config = _get_config()
        logger.info("✅ settings.yaml rechargé")
        
        # 2. Reset prompts.yaml (via singleton dans config_loader)
        try:
            from src.config_loader import reset_prompts_instance
            reset_prompts_instance()
        except ImportError:
            try:
                from config_loader import reset_prompts_instance
                reset_prompts_instance()
            except ImportError:
                logger.warning("config_loader non trouvé, prompts seront rechargés via LLM generator reset")
        logger.info("✅ prompts.yaml rechargé")
        
        # 3. Reset LLM generator (va recharger les prompts au prochain appel)
        _llm_generator = None
        logger.info("✅ LLM generator réinitialisé")
        
        # 4. Mettre à jour le reranker top_n SANS recharger le modèle GPU
        if _reranker is not None:
            new_top_n = new_config.get("retrieval", {}).get("rerank_top_n", 10)
            _reranker.top_n = new_top_n
            logger.info(f"✅ Reranker top_n mis à jour: {new_top_n}")
        
        # 5. Vider le cache des analyzers (seront recréés avec la nouvelle config)
        _analyzers.clear()
        logger.info("✅ Cache analyzers vidé")
        
        logger.info("🔄 Configuration rechargée avec succès !")
        return {
            "status": "ok",
            "message": "Configuration rechargée avec succès",
            "reloaded": ["settings.yaml", "prompts.yaml", "llm_generator", "analyzers"],
            "kept": ["embedder (GPU)", "reranker model (GPU)"]
        }
        
    except Exception as e:
        logger.error(f"❌ Erreur rechargement config: {e}")
        return {"status": "error", "message": str(e)}


@app.get("/api/health")
async def health_check():
    """Vérification de santé détaillée"""
    try:
        analyzer = get_analyzer()
        
        # Check LLM
        llm_ok = analyzer.llm_generator.health_check()
        
        return {
            "status": "healthy" if llm_ok else "degraded",
            "components": {
                "analyzer": "ok",
                "llm": "ok" if llm_ok else "unavailable",
                "cache": analyzer.get_cache_stats()
            }
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e)
        }


@app.get("/api/instance")
async def get_instance_info():
    """
    Expose l'identité de l'instance courante pour les UI.

    Retourne le nom de l'instance (depuis l'env INSTANCE_NAME), pour permettre
    aux UI d'afficher un bandeau et de masquer le sélecteur d'index.
    """
    return {
        "instance_name": INSTANCE_NAME,
        "single_index_mode": bool(INSTANCE_NAME) and not MULTI_INDEX_MODE,
    }


@app.get("/api/indexes")
async def list_indexes():
    """
    Liste les index disponibles (collections Qdrant et index OpenSearch).
    Permet à l'UI de proposer une sélection.

    Règle '1 instance = 1 index' : si INSTANCE_NAME est défini, on ne retourne
    QUE cet index (même s'il n'existe pas encore côté Qdrant/OpenSearch),
    pour masquer le sélecteur côté UI.
    """
    try:
        import httpx
        config = _get_config()

        # Mode mono-index : on retourne uniquement l'instance courante
        if INSTANCE_NAME and not MULTI_INDEX_MODE:
            return {
                "indexes": [{
                    "name": INSTANCE_NAME,
                    "type": "qdrant",
                    "opensearch_index": INSTANCE_NAME.lower(),
                    "has_opensearch": True,
                }],
                "default": INSTANCE_NAME,
                "instance_name": INSTANCE_NAME,
                "single_index_mode": not MULTI_INDEX_MODE,
            }

        indexes = []
        
        # Récupérer les collections Qdrant
        # Utiliser la variable d'environnement si disponible (pour Docker)
        try:
            qdrant_base = os.environ.get("QDRANT_URL", f"http://{config['qdrant']['host']}:{config['qdrant']['port']}")
            qdrant_url = f"{qdrant_base}/collections"
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(qdrant_url)
                if response.status_code == 200:
                    data = response.json()
                    for coll in data.get("result", {}).get("collections", []):
                        coll_name = coll.get("name", "")
                        if coll_name:
                            indexes.append({
                                "name": coll_name,
                                "type": "qdrant",
                                "opensearch_index": coll_name.lower()
                            })
        except Exception as e:
            logger.warning(f"Could not fetch Qdrant collections: {e}")
        
        # Récupérer les index OpenSearch (optionnel, pour vérification)
        # Utiliser la variable d'environnement si disponible (pour Docker)
        try:
            opensearch_base = os.environ.get("OPENSEARCH_URL", f"http://{config['opensearch']['host']}:{config['opensearch']['port']}")
            os_url = f"{opensearch_base}/_cat/indices?format=json"
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(os_url)
                if response.status_code == 200:
                    os_indexes = [idx.get("index") for idx in response.json() 
                                  if not idx.get("index", "").startswith(".")]
                    # Marquer les index qui ont une correspondance OpenSearch
                    for idx_info in indexes:
                        idx_info["has_opensearch"] = idx_info["opensearch_index"] in os_indexes
        except Exception as e:
            logger.warning(f"Could not fetch OpenSearch indexes: {e}")
        
        # Ne retourner le default que s'il existe dans les indexes
        default_name = config["qdrant"]["collection_name"]
        existing_names = [idx["name"] for idx in indexes]
        
        # Si le default n'existe pas, utiliser le premier index disponible ou None
        if default_name not in existing_names:
            default_name = existing_names[0] if existing_names else None
        
        return {
            "indexes": indexes,
            "default": default_name,
            "instance_name": None,
            "single_index_mode": False,
        }
    except Exception as e:
        return {
            "indexes": [],
            "default": None,
            "instance_name": INSTANCE_NAME,
            "single_index_mode": bool(INSTANCE_NAME) and not MULTI_INDEX_MODE,
            "error": str(e)
        }


@app.post("/api/analyze")
async def analyze(request: AnalyzeRequest):
    """
    Point d'entrée principal pour l'analyse de documents.
    
    Modes:
    - auto: Classification automatique de la requête
    - files: Recherche de fichiers spécifiques
    - folder: Analyse d'un dossier
    - cross: Analyse comparative
    - chat: Question générale avec RAG
    
    Args:
        request.index_name: Nom de l'index à interroger (optionnel)
    """
    try:
        # Utiliser l'index spécifié ou celui par défaut
        analyzer = get_analyzer(index_name=_resolve_index_name(request.index_name))
        
        scope = request.scope.dict() if request.scope else None
        options = request.options.dict() if request.options else None
        
        result = analyzer.analyze(
            query=request.query,
            mode=request.mode,
            scope=scope,
            options=options
        )
        
        # Ajouter l'info sur l'index utilisé
        result["index_name"] = _resolve_index_name(request.index_name)
        
        return result
        
    except Exception as e:
        logger.error(f"Analysis error: {e}")
        raise HTTPException(status_code=500, detail=str(e))






@app.post("/api/summarize-file")
async def summarize_file(request: SummarizeFileRequest):
    """
    Résume un fichier spécifique avec cache.
    """
    try:
        analyzer = get_analyzer()
        
        detail_level = "standard"
        if request.options:
            detail_level = request.options.detail_level
        
        result = analyzer.summarize_file(
            file_path=request.file_path,
            detail_level=detail_level
        )
        
        return result
        
    except Exception as e:
        logger.error(f"Summarize error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/cross-analyze")
async def cross_analyze(request: CrossAnalyzeRequest):
    """
    Analyse comparative entre plusieurs fichiers/dossiers.
    """
    try:
        analyzer = get_analyzer()
        
        options = request.options.dict() if request.options else None
        
        result = analyzer.analyze(
            query=request.query,
            mode="cross",
            scope=request.scope.dict(),
            options=options
        )
        
        return result
        
    except Exception as e:
        logger.error(f"Cross-analysis error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/classify")
async def classify_query(query: str):
    """
    Classifie une requête sans l'exécuter.
    Utile pour debug ou prévisualisation.
    """
    try:
        from src.agent.classifier import QueryClassifier
        classifier = QueryClassifier()
        result = classifier.classify(query)
        return result
        
    except Exception as e:
        logger.error(f"Classification error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Deep Search - Comparaison des résultats
# ============================================================================





@app.post("/api/query2")
async def iterative_query(request: QueryRequest):
    """
    Pipeline iteratif query2.

    Le contrat accepte query, index_name, top_k, custom_prompt et history.
    deep_search est ignore.

    Apres la recherche classique, une analyse de couverture (1 appel LLM
    sur les passages complets) determine si les passages suffisent. Sinon,
    une seconde recherche ciblee (requetes proposees par l'analyse,
    vocabulaire metier) complete les passages avant la generation finale.

    La reponse inclut une cle "iterative" tracant la couverture.
    """
    try:
        history_dicts = None
        if request.history and len(request.history) > 0:
            history_dicts = [{"role": msg.role, "content": msg.content} for msg in request.history]

        analyzer = get_analyzer(index_name=_resolve_index_name(request.index_name))
        # Reglages query2 relus a chaque requete : un reload-config a
        # chaud (UI feedback) les applique sans redemarrage.
        pipeline = IterativePipeline(
            analyzer,
            query2_config=_get_config().get("query2", {}),
        )

        start_time = time.time()
        result = pipeline.run(
            query=request.query,
            top_k=request.top_k,
            custom_prompt=request.custom_prompt,
            history=history_dicts,
            max_rounds=1,
        )
        elapsed_ms = int((time.time() - start_time) * 1000)

        search_results_raw = result.get("search_results", [])
        passages = []
        for chunk in search_results_raw[:30]:
            p = {
                "text": (chunk.get("text") or chunk.get("content") or "")[:1000],
                "file_name": chunk.get("file_name", ""),
                "score": round(chunk.get("rrf_score", chunk.get("score", 0)) or 0, 4),
            }
            meta = chunk.get("metadata", {}) or {}
            _reporter_pages(p, meta)
            if meta.get("section"):
                p["section"] = meta["section"]
            passages.append(p)

        response_data = {
            "response": result.get("response", ""),
            "sources": result.get("sources", []),
            "passages": passages,
            "mode": "chat_iteratif_v2",
            "index_name": _resolve_index_name(request.index_name),
            "iterative": result.get("iterative", {}),
            "processing_time_ms": elapsed_ms,
        }

        _log_query(
            question=request.query,
            answer=response_data.get("response", ""),
            sources=response_data.get("sources", []),
            index_name=response_data.get("index_name", ""),
            processing_time_ms=elapsed_ms,
            search_results=search_results_raw,
        )

        return response_data

    except Exception as e:
        logger.error(f"Query2 error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/query-history")
async def get_query_history(limit: int = 50):
    """Returns recent queries for RAGAS evaluation in Admin UI."""
    try:
        with sqlite3.connect(_QUERY_HISTORY_DB) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, timestamp, question, answer, contexts, index_name, processing_time_ms "
                "FROM query_history ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return {"queries": [dict(r) for r in rows]}
    except Exception as e:
        return {"queries": [], "error": str(e)}


# ============================================================================
# Agent Profiles & Runs (pour l'onglet Agents de l'UI Admin)
# ============================================================================















@app.get("/api/cache/stats")
async def cache_stats():
    """Statistiques du cache"""
    try:
        analyzer = get_analyzer()
        return analyzer.get_cache_stats()
    except Exception as e:
        return {"error": str(e)}


@app.delete("/api/cache")
async def clear_cache(file_path: Optional[str] = None):
    """Vide le cache (tout ou pour un fichier)"""
    try:
        analyzer = get_analyzer()
        analyzer.clear_cache(file_path)
        return {"status": "cleared", "file_path": file_path}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Feedback Management
# ============================================================================

_FEEDBACK_DB = os.environ.get("FEEDBACK_DB_PATH", "/app/feedbacks/feedbacks.db")


def _init_feedback_db():
    db_path = Path(_FEEDBACK_DB)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedbacks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            user_id TEXT,
            query TEXT NOT NULL,
            response TEXT NOT NULL,
            sources TEXT,
            index_name TEXT,
            feedback TEXT CHECK(feedback IN ('up', 'down')),
            expected_response TEXT,
            comment TEXT,
            processing_time_ms INTEGER
        )
    """)
    conn.commit()
    conn.close()


_init_feedback_db()


@app.get("/api/feedback/config")
async def get_feedback_config():
    """Retourne la configuration feedback (enabled, key_users) depuis settings.yaml."""
    config = _get_config()
    fb = config.get("feedback", {})
    return {
        "enabled": fb.get("enabled", False),
        "key_users": fb.get("key_users", []),
    }


class FeedbackSubmit(BaseModel):
    query: str
    response: str
    sources: Optional[str] = None
    index_name: Optional[str] = None
    feedback: str
    expected_response: Optional[str] = None
    comment: Optional[str] = None
    processing_time_ms: Optional[int] = None
    user_id: Optional[str] = None


@app.post("/api/feedback")
async def submit_feedback(fb: FeedbackSubmit):
    """Enregistre un feedback dans la base SQLite."""
    try:
        conn = sqlite3.connect(str(_FEEDBACK_DB))
        conn.execute(
            "INSERT INTO feedbacks (user_id, query, response, sources, index_name, feedback, expected_response, comment, processing_time_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (fb.user_id, fb.query, fb.response, fb.sources, fb.index_name,
             fb.feedback, fb.expected_response, fb.comment, fb.processing_time_ms),
        )
        conn.commit()
        fid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        logger.info(f"Feedback #{fid}: {fb.feedback} by {fb.user_id}")
        return {"status": "success", "id": fid}
    except Exception as e:
        logger.error(f"Feedback error: {e}")
        return {"status": "error", "message": str(e)}


# ============================================================================
# Modèle LLM actif + gestion dynamique (Ollama)
# ============================================================================
# Luciole Prime est bi-architecture :
#   - x86/AMD (Ollama)         : gestion dynamique des modèles (hot-swap).
#   - ARM64 GX10 (TensorRT-LLM): modèle figé au lancement du container.
# Le backend est déduit de LLM_URL par detect_llm_backend(). Les routes de
# gestion Ollama ci-dessous sont exposées uniquement lorsque le backend
# supporte le hot-swap ; sinon elles renvoient HTTP 501 Not Implemented.

_HOT_SWAP_LABELS = {
    "ollama": "Ollama",
    "tensorrt-llm": "TensorRT-LLM 1.2 (NVFP4)",
}


def _get_ollama_base_url() -> str:
    """URL de base du backend gérable (Ollama). Priorité : LLM_URL, OLLAMA_URL, config."""
    config = _get_config()
    url = (
        os.environ.get("LLM_URL")
        or os.environ.get("OLLAMA_URL")
        or config.get("llm", {}).get("base_url", "http://ollama:11434")
    )
    return url.rstrip("/")


def _require_hot_swap() -> None:
    """Garde : lève 501 si le backend courant ne supporte pas la gestion dynamique."""
    backend = detect_llm_backend()
    if not backend_supports_hot_swap(backend):
        raise HTTPException(
            status_code=501,
            detail=(
                f"Gestion dynamique des modèles non supportée par le backend "
                f"'{backend}'. Le modèle est figé au lancement du container "
                f"TensorRT-LLM ; relancez le container pour en changer."
            ),
        )


@app.get("/api/llm/model")
async def get_active_llm_model():
    """
    Retourne le modèle LLM actif, le backend détecté et s'il supporte le hot-swap.
    """
    config = _get_config()
    model_name = (
        os.environ.get("SERVED_MODEL_NAME")
        or config.get("llm", {}).get("model", "qwen3-30b-a3b-instruct")
    )
    llm_url = (
        os.environ.get("LLM_URL")
        or config.get("llm", {}).get("base_url", "http://tensorrt-llm:8000")
    )
    backend = detect_llm_backend(llm_url)
    supports_hot_swap = backend_supports_hot_swap(backend)
    return {
        "model": model_name,
        "backend": backend,
        "backend_label": _HOT_SWAP_LABELS.get(backend, backend),
        "url": llm_url,
        "supports_hot_swap": supports_hot_swap,
        "dynamic_management": supports_hot_swap,
        "info": (
            "Gestion dynamique des modèles disponible via ce backend."
            if supports_hot_swap
            else "Le modèle est fixé au lancement du container TensorRT-LLM. "
                 "Pour changer de modèle, relancez le container avec la nouvelle image/config."
        ),
    }


@app.get("/api/ollama/models")
async def list_ollama_models():
    """Liste les modèles installés (Ollama) avec leurs métadonnées."""
    _require_hot_swap()
    base_url = _get_ollama_base_url()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{base_url}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            config = _get_config()
            active_model = config.get("llm", {}).get("model", "")
            models = []
            for m in data.get("models", []):
                name = m.get("name", "")
                entry = {
                    "name": name,
                    "size_mb": round(m.get("size", 0) / 1e6),
                    "modified_at": m.get("modified_at", ""),
                    "digest": m.get("digest", "")[:12],
                    "active": name == active_model,
                    "family": "",
                    "parameter_size": "",
                    "quantization": "",
                    "context_length": 0,
                }
                try:
                    show_resp = await client.post(f"{base_url}/api/show", json={"name": name})
                    if show_resp.status_code == 200:
                        show_data = show_resp.json()
                        details = show_data.get("details", {})
                        entry["family"] = details.get("family", "")
                        entry["parameter_size"] = details.get("parameter_size", "")
                        entry["quantization"] = details.get("quantization_level", "")
                        for key, val in show_data.get("model_info", {}).items():
                            if key.endswith(".context_length"):
                                entry["context_length"] = int(val)
                                break
                except Exception:
                    pass
                models.append(entry)
        return {"models": models, "active_model": active_model}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ollama list error: {e}")
        raise HTTPException(status_code=502, detail=f"Erreur communication Ollama: {e}")


@app.post("/api/ollama/pull")
async def pull_ollama_model(request: dict):
    """Télécharge un modèle Ollama (progression en SSE)."""
    _require_hot_swap()
    model_name = request.get("model", "").strip()
    if not model_name:
        raise HTTPException(status_code=400, detail="Nom du modele requis")
    base_url = _get_ollama_base_url()
    logger.info(f"Pulling Ollama model: {model_name}")

    async def stream_pull():
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=30, read=3600, write=30, pool=30)
            ) as client:
                async with client.stream(
                    "POST", f"{base_url}/api/pull", json={"name": model_name}
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        yield f"data: {json.dumps({'error': body.decode()})}\n\n"
                        return
                    async for line in resp.aiter_lines():
                        if line.strip():
                            try:
                                progress = json.loads(line)
                                total = progress.get("total", 0)
                                completed = progress.get("completed", 0)
                                status = progress.get("status", "")
                                pct = int(completed / total * 100) if total > 0 else 0
                                yield (
                                    "data: "
                                    + json.dumps({
                                        "status": status,
                                        "pct": pct,
                                        "completed_mb": round(completed / 1e6),
                                        "total_mb": round(total / 1e6),
                                    })
                                    + "\n\n"
                                )
                            except json.JSONDecodeError:
                                pass
            yield f"data: {json.dumps({'status': 'done', 'pct': 100})}\n\n"
        except Exception as e:
            logger.error(f"Ollama pull error: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(stream_pull(), media_type="text/event-stream")


class OllamaActivateRequest(BaseModel):
    model: str
    num_ctx: int = 8192
    max_tokens: int = 4096


@app.post("/api/ollama/activate")
async def activate_ollama_model(request: OllamaActivateRequest):
    """Active un modèle installé (écrit dans settings.yaml + reload)."""
    _require_hot_swap()
    config_path = os.environ.get("CONFIG_PATH", "config/settings.yaml")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            settings = yaml.safe_load(f.read())
        old_model = settings.get("llm", {}).get("model", "")
        settings["llm"]["model"] = request.model
        settings["llm"]["num_ctx"] = request.num_ctx
        settings["llm"]["max_tokens"] = request.max_tokens
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(settings, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        logger.info(f"LLM model changed: {old_model} -> {request.model} (num_ctx={request.num_ctx})")
        result = await reload_config()
        return {
            "status": "ok",
            "old_model": old_model,
            "new_model": request.model,
            "num_ctx": request.num_ctx,
            "max_tokens": request.max_tokens,
            "reload": result,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Activate model error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/ollama/search")
async def search_ollama_registry(q: str = ""):
    """Recherche des modèles sur le registre public ollama.com."""
    _require_hot_swap()
    query = q.strip()
    if not query:
        return {"models": [], "error": "Parametre q requis"}
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(
                f"https://ollama.com/search?q={query}",
                headers={"User-Agent": "Luciole/3.0"},
            )
            resp.raise_for_status()
            html = resp.text
        models = []
        items = re.split(r'<li\s+x-test-model', html)
        for item in items[1:]:
            name_match = re.search(r'x-test-search-response-title[^>]*>([^<]+)<', item)
            desc_match = re.search(r'text-neutral-800[^>]*>([^<]+)<', item)
            sizes = re.findall(r'x-test-size[^>]*>([^<]+)<', item)
            pulls_match = re.search(r'Pulls', item)
            pulls = ""
            if pulls_match:
                before = item[:pulls_match.start()]
                num_match = re.search(r'>\s*([\d,.]+[KMB]?)\s*$', before)
                if num_match:
                    pulls = num_match.group(1).strip()
            if name_match:
                models.append({
                    "name": name_match.group(1).strip(),
                    "description": desc_match.group(1).strip() if desc_match else "",
                    "tags": [s.strip() for s in sizes],
                    "pulls": pulls,
                })
        return {"models": models, "query": query}
    except httpx.ConnectError:
        return {"models": [], "error": "Pas de connexion internet. Utilisez la saisie manuelle."}
    except Exception as e:
        logger.error(f"Ollama search error: {e}")
        return {"models": [], "error": str(e)}


@app.delete("/api/ollama/models")
async def delete_ollama_model(request: dict):
    """Supprime un modèle installé (refuse le modèle actif)."""
    _require_hot_swap()
    model_name = request.get("model", "").strip()
    if not model_name:
        raise HTTPException(status_code=400, detail="Nom du modele requis")
    config = _get_config()
    active_model = config.get("llm", {}).get("model", "")
    if model_name == active_model:
        raise HTTPException(
            status_code=409,
            detail=f"Impossible de supprimer le modele actif ({model_name}). "
                   f"Activez un autre modele d'abord.",
        )
    base_url = _get_ollama_base_url()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request("DELETE", f"{base_url}/api/delete", json={"name": model_name})
            if resp.status_code != 200:
                detail = resp.text or f"Ollama a retourne {resp.status_code}"
                raise HTTPException(status_code=resp.status_code, detail=detail)
        logger.info(f"Ollama model deleted: {model_name}")
        return {"status": "ok", "deleted": model_name}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ollama delete error: {e}")
        raise HTTPException(status_code=502, detail=f"Erreur communication Ollama: {e}")


# ============================================================================
# Startup / Shutdown
# ============================================================================

@app.on_event("startup")
async def startup_event():
    logger.info("🦋 Luciole Agent starting...")
    # Pre-warm the analyzer (optional)
    # get_analyzer()


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("🦋 Luciole Agent shutting down...")


# ============================================================================
# Run with uvicorn
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    
    port = int(os.environ.get("AGENT_PORT", 8500))
    uvicorn.run(app, host="0.0.0.0", port=port)
