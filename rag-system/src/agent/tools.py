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


# Clefs de metadonnees conservees dans les resultats resumes transmis au
# planificateur. Tout ce qui n'est pas ici disparait avant l'orchestrateur.
#
# page_start et page_end y figurent depuis la correction de #24 : le
# drapeau `agent.observation_pages` demasquait ces clefs dans
# `_format_metadata`, mais elles etaient deja supprimees ici. Le drapeau
# etait donc sans effet, et l'affichage reste bien commande par lui,
# `OBSERVATION_METADATA_MASQUEES` les retirant quand il est inactif.
METADATA_RESUMEES = (
    "client", "editor", "technology", "product", "version",
    "support_type", "severity", "ticket_id", "projet",
    "phase", "thematique", "departement",
    "page_start", "page_end",
)


class ToolRegistry:
    """
    Registre des tools disponibles pour la boucle agentique.

    Un profil métier (agent_profiles.py) restreint la liste effective des
    tools exposés au LLM via `tools_allowed`. Le registre lui-même reste
    global et partagé entre profils : ajouter un tool ici le rend
    disponible à tout profil qui l'autorise explicitement.
    """

    def __init__(
        self,
        hybrid_search,
        llm_generator=None,
        reranker=None,
        use_reranker: bool = True,
        rerank_candidates: int = 30,
    ):
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
            use_reranker: interrupteur explicite du reranking (defaut True).
                A la difference de reranker=None (le modele n'est pas
                disponible), ce flag exprime une decision : desactiver le
                reranking alors que le modele est charge. Sert aux mesures
                A/B contre un jeu d'evaluation (recall@k avec et sans
                reranking sur exactement le meme index et le meme corpus).
            rerank_candidates: taille du vivier soumis au reranker (defaut
                30). Un reranker n'a d'interet que s'il peut repecher des
                documents mal classes par la fusion RRF : on demande donc
                `rerank_candidates` resultats a la recherche hybride, puis
                le cross-encoder n'en retient que `top_k`. Sans cela, on
                rerankerait top_k vers top_k, soit une simple permutation
                des documents deja selectionnes. Aligne l'agent sur le
                pipeline /api/query (DocumentAnalyzer : fusion_top_k=30
                candidats -> rerank_top_n=15 retenus).
        """
        self.hybrid_search = hybrid_search
        self.llm_generator = llm_generator
        self.reranker = reranker
        self.use_reranker = use_reranker
        self.rerank_candidates = max(1, int(rerank_candidates))
        self._escalations: List[Dict] = []
        self._no_answers: List[Dict] = []

        if reranker is not None and not use_reranker:
            logger.warning(
                "ToolRegistry: reranker charge mais desactive via use_reranker=False "
                "(les recherches de l'agent renverront l'ordre RRF brut)"
            )

        # Table nom -> (fonction, description, schéma d'arguments)
        # La description et le schéma sont injectés tels quels dans le
        # prompt de planification (orchestrator.py), donc les garder
        # concis et sans ambiguïté pour le LLM.
        self._tools = {
            "search_documents": {
                "fn": self.search_documents,
                "description": (
                    "Recherche hybride (BM25 + dense) dans le corpus documentaire. "
                    "La partie dense encode la requête ENTIÈRE : une phrase "
                    "complète reprenant les termes de la question donne de bien "
                    "meilleurs résultats qu'une suite de mots-clés. "
                    "Utilise `filters` pour cibler des champs métier connus "
                    "(client, editor, technology, product, version, support_type, "
                    "severity, ticket_id, projet, phase, thematique, departement)."
                ),
                "args_schema": {
                    "query": (
                        "str (obligatoire) - une phrase complète en langage "
                        "naturel, reprenant les termes de la question de "
                        "l'utilisateur. N'envoie PAS une liste de mots-clés "
                        "télégraphiques : cela dégrade fortement la recherche."
                    ),
                    "filters": "dict (optionnel) - ex: {\"editor\": \"fortinet\"}",
                    "top_k": "int (optionnel, défaut 10)",
                },
            },
            "search_multi": {
                "fn": self.search_multi,
                "description": (
                    "Recherche hybride avec plusieurs formulations de la même "
                    "question, dédupliquée par meilleur score. À utiliser quand "
                    "une seule requête ne suffit pas à couvrir le besoin. "
                    "Chaque variante doit être une phrase complète, pas une "
                    "suite de mots-clés."
                ),
                "args_schema": {
                    "queries": (
                        "list[str] (obligatoire) - variantes formulées en "
                        "phrases complètes, angles différents de la même "
                        "question (pas des troncatures les unes des autres)."
                    ),
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
            "no_answer": {
                "fn": self.no_answer,
                "description": (
                    "Termine la boucle en declarant que le corpus ne permet "
                    "pas de repondre. A appeler des que les extraits observes "
                    "ne traitent pas du sujet de la question, plutot que de "
                    "repondre de memoire ou de citer un document hors sujet. "
                    "N'exige aucune source."
                ),
                "args_schema": {
                    "reason": "str (obligatoire) - ce qui manque dans le corpus",
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
        if not self._reranking_enabled() or not results:
            # Troncature explicite : quand le reranking n'a pas lieu, le
            # vivier elargi demande a la recherche hybride (voir
            # _candidate_top_k) ne doit pas fuir vers l'appelant, qui
            # attend au plus top_k resultats.
            return results[:top_k], False
        try:
            return self.reranker.rerank(query, results)[:top_k], True
        except Exception as e:
            logger.warning(f"Reranking agent echoue, resultats RRF conserves: {e}")
            return results[:top_k], False

    def _reranking_enabled(self) -> bool:
        """Le reranking est actif : modele charge ET non desactive."""
        return bool(self.reranker) and bool(self.use_reranker)

    def _candidate_top_k(self, top_k: int) -> int:
        """Nombre de resultats a demander a la recherche hybride.

        Avec reranking actif, on elargit le vivier a `rerank_candidates`
        pour que le cross-encoder puisse repecher des documents mal
        classes par RRF. Sans reranking, on demande exactement `top_k`
        (comportement inchange, pas de cout inutile).
        """
        if not self._reranking_enabled():
            return top_k
        return max(top_k, self.rerank_candidates)

    def search_documents(
        self,
        query: str,
        filters: Optional[Dict] = None,
        top_k: int = 10,
    ) -> Dict:
        if not query or not query.strip():
            raise ToolError("search_documents: 'query' ne peut pas être vide")

        results = self.hybrid_search.search(
            query, top_k=self._candidate_top_k(top_k), filters=filters
        )
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

        results = self.hybrid_search.search_multi(
            queries, top_k=self._candidate_top_k(top_k), filters=filters
        )
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

    def no_answer(self, reason: str) -> Dict:
        """Declare que le corpus ne permet pas de repondre.

        Sortie honnete distincte de l'escalade : elle constate une lacune
        documentaire sans reclamer d'intervention humaine. Les appels sont
        conserves pour permettre de compter les lacunes du corpus.
        """
        if not reason or not reason.strip():
            raise ToolError("no_answer: 'reason' ne peut pas être vide")

        entry = {"reason": reason.strip()}
        self._no_answers.append(entry)
        logger.info(f"Aucune reponse dans le corpus: {reason.strip()}")
        return {"no_answer": True, "reason": reason.strip()}

    def get_no_answers(self) -> List[Dict]:
        """
        Lacunes du corpus cumulees depuis la creation du registre.

        Meme portee processus que get_escalations : utile pour un comptage
        grossier en memoire, pas pour attribuer une lacune a une requete
        donnee. Un comptage durable devra passer par les logs ou la trace.
        """
        return list(self._no_answers)

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
                    if k in METADATA_RESUMEES
                },
            })
        return summarized

    def get_escalations(self) -> List[Dict]:
        """
        Escalades cumulees depuis la creation du registre.

        ATTENTION : la portee est le processus, pas la requete. L'API met le
        registre en cache par index, cette liste couvre donc toutes les
        requetes servies depuis le demarrage. Pour savoir si UNE execution a
        escalade, lire sa trace, pas cette liste.
        """
        return list(self._escalations)

    def get_seen_results(self) -> List[Dict]:
        """Retourne les chunks bruts rencontres pendant cette execution
        (accumules par toutes les recherches successives de la boucle),
        pour affichage cote UI (ex: passages dans la sidebar du chat).
        Acces public volontaire plutot que de lire _seen_results
        directement depuis l'exterieur (api.py), pour ne pas coupler
        les appelants a un attribut prive."""
        if not hasattr(self, "_seen_results"):
            return []
        return list(self._seen_results.values())

    def reset_run_state(self) -> None:
        """Remet a zero l'etat accumule pendant une execution (escalades,
        chunks vus). A appeler en debut de chaque run() : le ToolRegistry
        est reutilise en singleton entre requetes (voir get_orchestrator()
        dans api.py), donc sans ce reset get_escalations()/get_seen_results()
        continueraient de renvoyer des donnees de requetes precedentes sur
        le meme index. Corrige un etat partage preexistant, decouvert en
        construisant l'exposition des passages pour /api/agent/run (PR C)."""
        self._escalations = []
        self._seen_results = {}
