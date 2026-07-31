# -*- coding: utf-8 -*-
"""
Tools - Registre d'outils pour la boucle agentique Luciole

Chaque tool est une fonction pure (ou méthode légère) avec :
- un nom stable utilisé dans les profils YAML (tools_allowed)
- un schéma d'arguments simple (dict)
- un résultat sérialisable en JSON, ajouté au contexte de l'orchestrateur

Les tools de recherche documentaire réutilisent directement HybridSearch
(voir retrieval/hybrid.py) avec le paramètre `filters` livré précédemment,
donc un profil métier peut restreindre par défaut ses recherches
(ex: {"client": "belacom"}) sans dupliquer de logique de filtrage ici.

Aucun tool n'a d'effet de bord irréversible : escalate_to_human se contente
de marquer la conversation, il n'envoie rien lui-même (l'envoi effectif
— email, ticket, notification — reste un module séparé à brancher plus tard).
"""

from typing import Any, Dict, List, Optional
from loguru import logger


class ToolError(Exception):
    """Erreur d'exécution d'un tool, capturée par l'orchestrateur et
    renvoyée au LLM comme observation (pas une exception fatale)."""
    pass


class ToolRegistry:
    """
    Registre des tools disponibles pour la boucle agentique.

    Un profil métier (agent_profiles.py) restreint la liste effective des
    tools exposés au LLM via `tools_allowed`. Le registre lui-même reste
    global et partagé entre profils : ajouter un tool ici le rend
    disponible à tout profil qui l'autorise explicitement.
    """

    def __init__(self, hybrid_search, llm_generator=None, reranker=None):
        """
        Args:
            hybrid_search: instance de HybridSearch (retrieval/hybrid.py),
                déjà configurée pour l'index/instance courant.
            llm_generator: instance de LLMGenerator, optionnelle (réservée
                à d'éventuels tools futurs nécessitant un appel LLM dédié,
                ex: reformulation). Non utilisée par les tools actuels.
            reranker: instance de Reranker (retrieval/reranker.py),
                optionnelle. Si fournie, appliquée apres chaque recherche
                (search_documents/search_multi) pour aligner la qualite de
                retrieval de l'agent sur celle du pipeline /api/query, qui
                reranke deja systematiquement (voir DocumentAnalyzer). Si
                None ou en cas d'erreur, on retombe silencieusement sur
                l'ordre RRF brut (jamais d'exception fatale ici).
        """
        self.hybrid_search = hybrid_search
        self.llm_generator = llm_generator
        self.reranker = reranker
        self._escalations: List[Dict] = []

        # Table nom -> (fonction, description, schéma d'arguments)
        # La description et le schéma sont injectés tels quels dans le
        # prompt de planification (orchestrator.py), donc les garder
        # concis et sans ambiguïté pour le LLM.
        self._tools = {
            "search_documents": {
                "fn": self.search_documents,
                "description": (
                    "Recherche hybride (BM25 + dense) dans le corpus documentaire. "
                    "Utilise `filters` pour cibler des champs métier connus "
                    "(client, editor, technology, product, version, support_type, "
                    "severity, ticket_id, projet, phase, thematique, departement)."
                ),
                "args_schema": {
                    "query": "str (obligatoire) - la requête de recherche",
                    "filters": "dict (optionnel) - ex: {\"editor\": \"fortinet\"}",
                    "top_k": "int (optionnel, défaut 10)",
                },
            },
            "search_multi": {
                "fn": self.search_multi,
                "description": (
                    "Recherche hybride avec plusieurs formulations de la même "
                    "question, dédupliquée par meilleur score. À utiliser quand "
                    "une seule requête ne suffit pas à couvrir le besoin."
                ),
                "args_schema": {
                    "queries": "list[str] (obligatoire) - variantes de la requête",
                    "filters": "dict (optionnel)",
                    "top_k": "int (optionnel, défaut 10)",
                },
            },
            "get_document": {
                "fn": self.get_document,
                "description": (
                    "Récupère les chunks déjà retrouvés dans cette exécution pour "
                    "un fichier précis (file_path ou file_name), pour approfondir "
                    "un document déjà identifié par une recherche précédente."
                ),
                "args_schema": {
                    "file_name_or_path": "str (obligatoire)",
                },
            },
            "escalate_to_human": {
                "fn": self.escalate_to_human,
                "description": (
                    "Marque la conversation comme nécessitant une validation "
                    "humaine (ex: sévérité critique, sources insuffisantes, "
                    "demande hors périmètre). N'envoie rien automatiquement, "
                    "signale seulement l'escalade pour traitement ultérieur."
                ),
                "args_schema": {
                    "reason": "str (obligatoire) - motif de l'escalade",
                },
            },
            "final_answer": {
                "fn": self.final_answer,
                "description": (
                    "Termine la boucle et fournit la réponse finale à "
                    "l'utilisateur. Doit être appelé dès que suffisamment "
                    "d'informations ont été rassemblées. Les sources doivent "
                    "correspondre à des documents réellement retournés par "
                    "search_documents/search_multi dans cette exécution."
                ),
                "args_schema": {
                    "text": "str (obligatoire) - la réponse finale",
                    "sources": "list[str] (optionnel) - file_path des sources citées",
                },
            },
        }

    # =========================================================================
    # INTROSPECTION (utilisée par l'orchestrateur pour construire le prompt)
    # =========================================================================

    def available_tools(self, allowed: Optional[List[str]] = None) -> Dict[str, Dict]:
        """
        Retourne les tools disponibles, restreints à `allowed` si fourni.

        Args:
            allowed: liste de noms de tools autorisés (depuis le profil
                métier actif). None = tous les tools du registre.

        Returns:
            dict {nom_tool: {"description": ..., "args_schema": ...}}
            (sans la fonction elle-même, pour usage direct dans un prompt)
        """
        names = allowed if allowed is not None else list(self._tools.keys())
        return {
            name: {
                "description": spec["description"],
                "args_schema": spec["args_schema"],
            }
            for name, spec in self._tools.items()
            if name in names
        }

    def has_tool(self, name: str, allowed: Optional[List[str]] = None) -> bool:
        if name not in self._tools:
            return False
        if allowed is not None and name not in allowed:
            return False
        return True

    def call(self, name: str, args: Dict[str, Any], allowed: Optional[List[str]] = None) -> Any:
        """
        Exécute un tool par son nom.

        Raises:
            ToolError: si le tool est inconnu, non autorisé pour ce profil,
                ou si son exécution échoue.
        """
        if name not in self._tools:
            raise ToolError(f"Tool inconnu: '{name}'")
        if allowed is not None and name not in allowed:
            raise ToolError(f"Tool '{name}' non autorisé pour ce profil")

        fn = self._tools[name]["fn"]
        try:
            return fn(**(args or {}))
        except ToolError:
            raise
        except TypeError as e:
            raise ToolError(f"Arguments invalides pour '{name}': {e}")
        except Exception as e:
            logger.error(f"Erreur exécution tool '{name}': {e}")
            raise ToolError(f"Erreur lors de l'exécution de '{name}': {e}")

    # =========================================================================
    # IMPLÉMENTATION DES TOOLS
    # =========================================================================

    def _rerank(self, query: str, results: List[Dict], top_k: int) -> tuple:
        """Applique le reranker si disponible.

        Ne leve jamais d'exception a ce niveau (un echec ponctuel du
        modele pendant l'inference ne doit pas faire crasher une
        recherche), mais renvoie explicitement si le reranking a eu
        lieu ou non (was_reranked) pour que ce soit visible dans le
        resultat du tool et donc dans la trace de l'agent. A la
        difference de l'absence de reranker au demarrage (bloquante
        par defaut, voir _get_reranker), ceci couvre uniquement :
        - le mode degrade explicitement autorise (RERANKER_OPTIONAL=true)
        - un echec transitoire de l'appel rerank() lui-meme
        Dans les deux cas l'anomalie doit rester visible, pas masquee.
        """
        if not self.reranker or not results:
            return results, False
        try:
            return self.reranker.rerank(query, results)[:top_k], True
        except Exception as e:
            logger.warning(f"Reranking agent echoue, resultats RRF conserves: {e}")
            return results, False

    def search_documents(
        self,
        query: str,
        filters: Optional[Dict] = None,
        top_k: int = 10,
    ) -> Dict:
        if not query or not query.strip():
            raise ToolError("search_documents: 'query' ne peut pas être vide")

        results = self.hybrid_search.search(query, top_k=top_k, filters=filters)
        results, was_reranked = self._rerank(query, results, top_k)
        self._remember_results(results)
        return {
            "count": len(results),
            "results": self._summarize_results(results),
            "reranked": was_reranked,
        }

    def search_multi(
        self,
        queries: List[str],
        filters: Optional[Dict] = None,
        top_k: int = 10,
    ) -> Dict:
        if not queries:
            raise ToolError("search_multi: 'queries' ne peut pas être vide")
        if not hasattr(self.hybrid_search, "search_multi"):
            raise ToolError("search_multi non supporté par ce moteur de recherche")

        results = self.hybrid_search.search_multi(queries, top_k=top_k, filters=filters)
        results, was_reranked = self._rerank(queries[0], results, top_k)
        self._remember_results(results)
        return {
            "count": len(results),
            "results": self._summarize_results(results),
            "reranked": was_reranked,
        }

    def get_document(self, file_name_or_path: str) -> Dict:
        if not file_name_or_path:
            raise ToolError("get_document: 'file_name_or_path' ne peut pas être vide")

        needle = file_name_or_path.strip().lower()
        matches = [
            r for r in self._seen_results.values()
            if needle in (r.get("file_path", "") or "").lower()
            or needle in (r.get("file_name", "") or "").lower()
        ] if hasattr(self, "_seen_results") else []

        if not matches:
            return {
                "count": 0,
                "results": [],
                "note": (
                    "Aucun chunk déjà retrouvé pour ce document dans cette "
                    "exécution. Utilise search_documents pour le retrouver."
                ),
            }
        return {"count": len(matches), "results": self._summarize_results(matches)}

    def escalate_to_human(self, reason: str) -> Dict:
        if not reason or not reason.strip():
            raise ToolError("escalate_to_human: 'reason' ne peut pas être vide")

        entry = {"reason": reason.strip()}
        self._escalations.append(entry)
        logger.warning(f"🚨 Escalade humaine demandée par l'agent: {reason}")
        return {"escalated": True, "reason": reason.strip()}

    def final_answer(self, text: str, sources: Optional[List[str]] = None) -> Dict:
        if not text or not text.strip():
            raise ToolError("final_answer: 'text' ne peut pas être vide")
        return {"text": text.strip(), "sources": sources or []}

    # =========================================================================
    # ÉTAT INTERNE (mémoire courte de l'exécution, pas de persistance)
    # =========================================================================

    def _remember_results(self, results: List[Dict]) -> None:
        """Garde en mémoire les chunks retrouvés dans cette exécution pour
        que get_document puisse les réutiliser sans refaire une recherche."""
        if not hasattr(self, "_seen_results"):
            self._seen_results: Dict[str, Dict] = {}
        for r in results or []:
            key = r.get("chunk_id") or r.get("file_path") or r.get("file_name")
            if key:
                self._seen_results[key] = r

    @staticmethod
    def _summarize_results(results: List[Dict], max_chars: int = 500) -> List[Dict]:
        """Réduit les résultats à ce qui est utile au LLM planificateur
        (évite de saturer le contexte avec des chunks entiers à chaque étape)."""
        summarized = []
        for r in results or []:
            text = r.get("text", "") or ""
            summarized.append({
                "file_name": r.get("file_name", ""),
                "file_path": r.get("file_path", ""),
                "score": round(r.get("rrf_score", r.get("score", 0)) or 0, 4),
                "excerpt": text[:max_chars] + ("..." if len(text) > max_chars else ""),
                "metadata": {
                    k: v for k, v in (r.get("metadata") or {}).items()
                    if k in (
                        "client", "editor", "technology", "product", "version",
                        "support_type", "severity", "ticket_id", "projet",
                        "phase", "thematique", "departement",
                    )
                },
            })
        return summarized

    def get_escalations(self) -> List[Dict]:
        """Retourne les escalades déclenchées pendant cette exécution."""
        return list(self._escalations)
