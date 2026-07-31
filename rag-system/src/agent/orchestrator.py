# -*- coding: utf-8 -*-
"""
AgentOrchestrator - Boucle agentique bornée pour Luciole

Principe : une boucle plan -> act -> observe, bornée par max_steps et par
un registre de tools fixe (voir tools.py), pas un agent autonome libre.
À chaque étape, le LLM planificateur choisit UN tool à appeler (ou répond
directement via final_answer) en se basant sur les observations
accumulées. La boucle s'arrête dès que final_answer est appelé, que
max_steps est atteint, ou qu'une erreur non récupérable survient.

Cette conception reprend la doctrine actée pour Luciole : séparation
extraction/recherche/rédaction, critères d'arrêt explicites, RAG comme
tool itératif plutôt que pipeline statique.
"""

import json
import re
import time
from typing import Any, Dict, List, Optional

from loguru import logger

from .tools import ToolRegistry, ToolError


DEFAULT_MAX_STEPS = 5

# Budget d'injection du contenu retrouve dans les observations transmises au
# planificateur. Sans extraits, le LLM ne voit que des noms de fichiers : il
# redige alors sa reponse sans avoir jamais lu les documents, et fabrique le
# contenu qu'il attribue a des sources pourtant reellement remontees.
OBSERVATION_MAX_RESULTS = 5
OBSERVATION_EXCERPT_CHARS = 400
OBSERVATION_METADATA_MAX = 6
# Les observations etant desormais riches, le repli de fin de boucle doit etre
# borne : il est affiche tel quel a l'utilisateur.
FALLBACK_OBSERVATION_CHARS = 800
OBSERVATION_METADATA_PRIORITY = (
    "ticket_id", "product", "version", "technology", "support_type",
    "severity", "projet", "phase", "thematique", "departement",
    "editor", "client",
)

# Sorties terminales de la boucle, toujours proposees au LLM meme si le
# profil client ne les declare pas : sans elles il n'a aucun moyen propre
# de conclure et epuise max_steps.
TERMINAL_TOOLS_ALWAYS_AVAILABLE = ("final_answer", "no_answer")

# Message rendu a l'utilisateur quand le corpus ne permet pas de repondre.
# Texte fixe et non negociable : le motif redige par le LLM part dans la
# trace, pas dans la reponse, pour qu'aucun contenu non sourcé ne remonte.
NO_ANSWER_MESSAGE = (
    "Le corpus documentaire ne contient pas d'information permettant de "
    "répondre à cette question."
)

ESCALATION_MESSAGE = (
    "Cette demande nécessite une validation humaine, une escalade a été "
    "enregistrée."
)


class AgentOrchestrator:
    """
    Exécute une boucle agentique bornée pour répondre à une requête
    utilisateur, en s'appuyant sur un profil métier (tools autorisés,
    prompt système, conditions d'arrêt) et un ToolRegistry partagé.
    """

    def __init__(self, tool_registry: ToolRegistry, llm_generator, query_rewriter=None):
        """
        Args:
            tool_registry: instance de ToolRegistry (tools.py)
            llm_generator: instance de LLMGenerator (generation/llm.py),
                utilisée uniquement via call_llm() pour la planification
                (pas generate(), qui est le chemin RAG procédural existant)
            query_rewriter: instance de QueryRewriter (retrieval/query_rewriter.py),
                optionnelle. Si fournie, appliquée une seule fois en debut de
                run() (pas a chaque etape de la boucle) pour aligner l'agent
                sur le meme comportement de reformulation que le pipeline
                classique /api/query. Si None, l'agent recoit la question
                telle quelle (comportement pre-correctif).
        """
        self.tools = tool_registry
        self.llm = llm_generator
        self.query_rewriter = query_rewriter

    def run(
        self,
        query: str,
        profile: Dict,
        history: Optional[List[Dict]] = None,
        deep_search: bool = False,
    ) -> Dict:
        """
        Point d'entree public. Si deep_search=True et qu'un history non
        vide est fourni, delegue a _run_deep_search (double passage,
        etape pre-boucle explicite, jamais choisie par le LLM
        planificateur). Sinon delegue a _run_single_pass (comportement
        historique, inchange).
        """
        if deep_search and history:
            return self._run_deep_search(query, profile, history)
        return self._run_single_pass(query, profile, history)

    def _run_deep_search(self, query: str, profile: Dict, history: List[Dict]) -> Dict:
        """
        Double passage complet de la boucle agentique (sans puis avec
        historique), puis selection du meilleur via une heuristique
        identique dans l'esprit a _compare_deep_search_results (pipeline
        classique, agent/api.py) : deux fois plus couteux qu'un passage
        normal (2x max_steps), donc pilote uniquement par ce parametre
        explicite, jamais par un tool que le LLM choisirait dynamiquement.
        """
        logger.info("Deep search (agent): lancement double passage (frais + contextuel)")
        result_fresh = self._run_single_pass(query, profile, history=None)
        result_context = self._run_single_pass(query, profile, history=history)

        best, choice = self._pick_deep_search_result(result_fresh, result_context)
        logger.info(f"Deep search (agent): resultat retenu = {choice}")

        best = dict(best)
        best["deep_search"] = {
            "enabled": True,
            "choice": choice,
            "fresh_steps_used": result_fresh["steps_used"],
            "context_steps_used": result_context["steps_used"],
            "fresh_sources_count": len(result_fresh.get("sources") or []),
            "context_sources_count": len(result_context.get("sources") or []),
        }
        return best

    @staticmethod
    def _pick_deep_search_result(result_fresh: Dict, result_context: Dict):
        """
        Heuristique de selection pour le deep search de l'agent. Inspiree
        de _compare_deep_search_results (agent/api.py, pipeline classique)
        mais adaptee : l'agent n'a pas de score de confidence (final_answer
        ne produit que text/sources), la comparaison se limite donc aux
        patterns "pas d'info" et au nombre de sources. Copie locale plutot
        qu'import croise entre orchestrator.py et api.py.
        """
        no_info_patterns = [
            "pas d'information", "pas trouve", "n'ai pas trouve",
            "aucune information", "pas de donnees", "information non trouvee",
            "je ne dispose pas", "pas disponible dans", "documents ne contiennent pas",
        ]

        def has_no_info(text: str) -> bool:
            text_lower = (text or "").lower()
            return any(p in text_lower for p in no_info_patterns)

        response_fresh = result_fresh.get("response", "")
        response_context = result_context.get("response", "")
        fresh_no_info = has_no_info(response_fresh)
        context_no_info = has_no_info(response_context)

        if not fresh_no_info and context_no_info:
            return result_fresh, "fresh_found"
        if fresh_no_info and not context_no_info:
            return result_context, "context_found"
        if not fresh_no_info and not context_no_info:
            fresh_sources = len(result_fresh.get("sources") or [])
            context_sources = len(result_context.get("sources") or [])
            if fresh_sources >= context_sources:
                return result_fresh, "fresh_better_score"
            return result_context, "context_better_score"
        return result_fresh, "both_no_info"

    def _run_single_pass(
        self,
        query: str,
        profile: Dict,
        history: Optional[List[Dict]] = None,
    ) -> Dict:
        """
        Corps de la boucle agentique pour un seul passage (sans deep
        search). Exécute la boucle agentique pour une requête donnée.

        Args:
            query: requête utilisateur
            profile: profil métier chargé (voir agent_profiles.py), doit
                contenir au minimum tools_allowed, max_steps, system_prompt
            history: historique de conversation optionnel (liste de
                {"role": ..., "content": ...})

        Returns:
            dict avec:
                - result_type: "agentic"
                - response: texte de la réponse finale
                - sources: liste des sources citées (file_path)
                - escalated: bool, True si escalate_to_human a été appelé
                - escalation_reason: str ou None
                - trace: liste des étapes exécutées (tool, args, résultat,
                  durée), pour affichage dans l'UI admin
                - steps_used: nombre d'étapes effectivement exécutées
                - stopped_reason: "final_answer" | "max_steps" | "error"
        """
        max_steps = int(profile.get("max_steps", DEFAULT_MAX_STEPS))
        tools_allowed = profile.get("tools_allowed") or []
        system_prompt = profile.get("system_prompt", "").strip() or (
            "Tu es un agent Luciole. Utilise les outils disponibles pour "
            "rassembler des informations avant de répondre. Cite tes sources."
        )
        stop_conditions = profile.get("stop_conditions", {}) or {}

        available = self.tools.available_tools(allowed=tools_allowed)
        for exit_tool in TERMINAL_TOOLS_ALWAYS_AVAILABLE:
            if exit_tool not in available:
                # Sans ces sorties le LLM n'a aucun moyen propre de terminer
                # la boucle : on les rend toujours disponibles par securite.
                # no_answer en fait partie car les profils clients existants,
                # definis hors depot, ne le declarent pas encore.
                available = dict(
                    available,
                    **self.tools.available_tools(allowed=[exit_tool]),
                )
                tools_allowed = list(tools_allowed) + [exit_tool]

        trace: List[Dict] = []
        observations: List[str] = []
        rejected_final_answers: List[str] = []

        original_query = query
        rewritten_queries, query_type, was_rewritten = self._apply_query_rewriting(query)
        # Portes par self pendant la duree de cet appel run() pour que
        # _finalize()/_error_result() puissent les exposer sans modifier
        # chacun de leurs points de retour un par un (meme pattern que
        # self.tools.get_escalations() deja lu depuis _finalize).
        self._last_query_type = query_type
        self._last_was_rewritten = was_rewritten
        if was_rewritten:
            trace.append({
                "step": 0,
                "tool": "query_rewriting",
                "args": {"query": original_query},
                "result": {
                    "rewritten_queries": rewritten_queries,
                    "query_type": query_type,
                },
                "duration_ms": 0,
                "note": "etape_pre_boucle",
            })

        for step in range(1, max_steps + 1):
            plan_prompt = self._build_plan_prompt(
                query=original_query,
                rewritten_queries=rewritten_queries if was_rewritten else None,
                tools_description=available,
                observations=observations,
                history=history,
                step=step,
                max_steps=max_steps,
            )

            t0 = time.time()
            try:
                raw_decision = self.llm.call_llm(system_prompt, plan_prompt)
            except Exception as e:
                logger.error(f"Erreur LLM pendant la planification (étape {step}): {e}")
                return self._error_result(trace, f"Erreur LLM: {e}", step)

            decision = self._parse_decision(raw_decision)
            duration_ms = int((time.time() - t0) * 1000)

            if decision is None:
                # Réponse non parsable : on la traite comme réponse finale
                # brute plutôt que d'échouer bruyamment sur une boucle bornée.
                logger.warning(
                    f"Décision non parsable à l'étape {step}, traitée comme réponse finale brute"
                )
                trace.append({
                    "step": step,
                    "tool": "final_answer",
                    "args": {"text": raw_decision},
                    "result": {"text": raw_decision, "sources": []},
                    "duration_ms": duration_ms,
                    "note": "decision_non_parsable",
                })
                return self._finalize(
                    trace, raw_decision, [], step, "final_answer_fallback"
                )

            tool_name = decision.get("tool")
            tool_args = decision.get("args", {}) or {}

            if tool_name == "final_answer":
                try:
                    result = self.tools.call("final_answer", tool_args, allowed=tools_allowed)
                except ToolError as e:
                    result = {"text": str(tool_args.get("text", "")), "sources": []}
                    logger.warning(f"final_answer invalide, fallback: {e}")

                trace.append({
                    "step": step,
                    "tool": "final_answer",
                    "args": tool_args,
                    "result": result,
                    "duration_ms": duration_ms,
                })

                if not self._meets_stop_conditions(result, stop_conditions):
                    # Pas assez de sources / pas de citation alors que le
                    # profil l'exige : on redonne une chance en observation
                    # plutôt que de renvoyer une réponse non conforme.
                    answer_text = str(result.get("text", "")).strip()
                    is_repeat = answer_text in rejected_final_answers
                    rejected_final_answers.append(answer_text)

                    if is_repeat and "escalate_to_human" in tools_allowed:
                        # Le LLM a retenté la même réponse déjà refusée sans
                        # varier sa stratégie : inutile de consommer le reste
                        # de max_steps, on force une escalade explicite.
                        escalate_args = {
                            "reason": (
                                "Réponse finale répétée après refus (sources "
                                "insuffisantes ou citation manquante) sans "
                                "nouvelle recherche entre les deux tentatives."
                            )
                        }
                        try:
                            esc_result = self.tools.call(
                                "escalate_to_human", escalate_args, allowed=tools_allowed
                            )
                        except ToolError as e:
                            esc_result = {"escalated": False, "reason": str(e)}
                        trace.append({
                            "step": step,
                            "tool": "escalate_to_human",
                            "args": escalate_args,
                            "result": esc_result,
                            "duration_ms": 0,
                            "note": "auto_escalade_repetition_final_answer",
                        })
                        return self._finalize(
                            trace,
                            "Je n'ai pas assez de sources fiables pour répondre "
                            "avec certitude, une escalade vers un support humain "
                            "a été déclenchée.",
                            [], step, "escalated",
                        )

                    observations.append(
                        f"Ta réponse \"{answer_text[:200]}\" a été refusée : "
                        "elle ne respecte pas les conditions d'arrêt du profil "
                        "(sources insuffisantes ou citation manquante). Ne "
                        "répète pas ce texte tel quel : lance search_documents "
                        "avec une requête reformulée, essaie search_multi, ou "
                        "appelle no_answer si le corpus ne contient vraiment "
                        "pas la réponse. N'invente pas de source pour "
                        "satisfaire la condition."
                    )
                    continue

                return self._finalize(
                    trace, result.get("text", ""), result.get("sources", []),
                    step, "final_answer"
                )

            if tool_name in ("no_answer", "escalate_to_human"):
                # Sorties terminales au meme titre que final_answer. Traitees
                # auparavant comme des tools intermediaires, elles laissaient
                # la boucle continuer : le LLM escaladait, recevait son propre
                # accuse de reception en observation, et re-escaladait jusqu'a
                # epuiser max_steps.
                try:
                    result = self.tools.call(tool_name, tool_args, allowed=tools_allowed)
                    error = None
                except ToolError as e:
                    # Tool refuse par le profil ou argument manquant : on
                    # rend la main au LLM au lieu de terminer sur un echec.
                    trace.append({
                        "step": step,
                        "tool": tool_name,
                        "args": tool_args,
                        "result": None,
                        "error": str(e),
                        "duration_ms": duration_ms,
                    })
                    observations.append(f"Erreur lors de l'appel à {tool_name}: {e}")
                    continue

                trace.append({
                    "step": step,
                    "tool": tool_name,
                    "args": tool_args,
                    "result": result,
                    "error": error,
                    "duration_ms": duration_ms,
                })
                message = (
                    NO_ANSWER_MESSAGE if tool_name == "no_answer"
                    else ESCALATION_MESSAGE
                )
                reason = "no_answer" if tool_name == "no_answer" else "escalated"
                return self._finalize(trace, message, [], step, reason)

            # Tool intermédiaire (search_documents, get_document, etc.)
            try:
                tool_result = self.tools.call(tool_name, tool_args, allowed=tools_allowed)
                observation = self._format_observation(tool_name, tool_result)
                error = None
            except ToolError as e:
                tool_result = None
                observation = f"Erreur lors de l'appel à {tool_name}: {e}"
                error = str(e)

            trace.append({
                "step": step,
                "tool": tool_name,
                "args": tool_args,
                "result": tool_result,
                "error": error,
                "duration_ms": duration_ms,
            })
            observations.append(observation)

        # max_steps atteint sans final_answer
        logger.warning(f"Boucle agentique arrêtée: max_steps ({max_steps}) atteint sans final_answer")
        if observations:
            fallback_text = (
                "Je n'ai pas pu formuler une réponse complète dans le nombre "
                "d'étapes disponibles. Voici ce que j'ai trouvé jusqu'ici : "
                + self._truncate(" ".join(observations[-2:]),
                                 FALLBACK_OBSERVATION_CHARS)
            )
        else:
            fallback_text = "Je n'ai pas trouvé suffisamment d'informations pour répondre."
        return self._finalize(trace, fallback_text, [], max_steps, "max_steps")

    # =========================================================================
    # CONSTRUCTION DU PROMPT DE PLANIFICATION
    # =========================================================================

    def _apply_query_rewriting(self, query: str):
        """
        Applique la reformulation de requete une seule fois, avant le
        premier tour de planification. Ne modifie jamais la query envoyee
        au LLM planificateur comme "question de l'utilisateur" (on garde
        toujours l'original pour la fidelite/tracabilite) : les variantes
        reformulees sont ajoutees en complement dans le prompt pour guider
        les appels a search_documents / search_multi.

        Returns:
            (rewritten_queries: List[str], query_type: str, was_rewritten: bool)
            was_rewritten est False si aucun rewriter n'est configure, si la
            requete est vide, ou si le rewriter n'a rien modifie.
        """
        if self.query_rewriter is None:
            return [query], "general", False

        try:
            rewritten_queries, query_type, was_rewritten = self.query_rewriter.rewrite(query)
        except Exception as e:
            # Coherent avec la doctrine "pas de mode degrade silencieux" :
            # on log l'echec mais on ne casse jamais la boucle agentique
            # pour un probleme de reformulation, qui est une optimisation,
            # pas une dependance critique comme le reranker.
            logger.warning(f"Echec du query rewriting, question originale conservee: {e}")
            return [query], "general", False

        return rewritten_queries, query_type, was_rewritten

    def _build_plan_prompt(
        self,
        query: str,
        tools_description: Dict[str, Dict],
        observations: List[str],
        history: Optional[List[Dict]],
        step: int,
        max_steps: int,
        rewritten_queries: Optional[List[str]] = None,
    ) -> str:
        tools_block = "\n".join(
            f"- {name}: {spec['description']}\n  Arguments: {spec['args_schema']}"
            for name, spec in tools_description.items()
        )

        history_block = ""
        if history:
            history_lines = [
                f"{h.get('role', 'user')}: {h.get('content', '')}"
                for h in history[-4:]  # contexte court, pas tout l'historique
            ]
            history_block = "Historique récent:\n" + "\n".join(history_lines) + "\n\n"

        observations_block = ""
        if observations:
            observations_block = "Observations des étapes précédentes:\n" + "\n".join(
                f"{i+1}. {obs}" for i, obs in enumerate(observations)
            ) + "\n\n"

        rewritten_block = ""
        if rewritten_queries and len(rewritten_queries) > 1:
            variants = "\n".join(f"- {q}" for q in rewritten_queries)
            rewritten_block = (
                "Reformulations suggérées (mêmes outils, essaie ces variantes "
                "avec search_multi si une seule recherche ne suffit pas):\n"
                f"{variants}\n\n"
            )
        elif rewritten_queries and rewritten_queries[0] != query:
            rewritten_block = (
                f"Reformulation suggérée pour la recherche: {rewritten_queries[0]}\n\n"
            )

        return f"""{history_block}Question de l'utilisateur: {query}

{rewritten_block}Outils disponibles:
{tools_block}

{observations_block}Étape {step}/{max_steps}.

Choisis UN SEUL outil à appeler maintenant, en fonction de ce que tu sais déjà.
Si tu as assez d'informations pour répondre, appelle final_answer.

Règles impératives:
- N'utilise jamais tes connaissances générales. Chaque affirmation de ta
  réponse finale doit provenir des extraits observés ci-dessus.
- Si les extraits ne traitent pas du sujet de la question, appelle no_answer.
  Ne cite jamais un document qui ne contient pas la réponse : une citation
  exacte accolée à un contenu inventé est la pire sortie possible.
- Ne relance pas une recherche déjà effectuée à l'identique. Si une
  reformulation n'a rien donné non plus, conclus avec no_answer.

Réponds UNIQUEMENT avec un objet JSON de la forme:
{{"tool": "nom_de_l_outil", "args": {{...}}}}

Aucun texte avant ou après le JSON."""

    # =========================================================================
    # PARSING DE LA DÉCISION DU LLM
    # =========================================================================

    @staticmethod
    def _parse_decision(raw: str) -> Optional[Dict]:
        """
        Extrait {"tool": ..., "args": {...}} de la réponse brute du LLM.
        Tolère du texte autour du JSON (certains modèles ajoutent des
        explications malgré la consigne).
        """
        if not raw or not raw.strip():
            return None

        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None

        try:
            parsed = json.loads(match.group())
        except json.JSONDecodeError:
            return None

        if not isinstance(parsed, dict) or "tool" not in parsed:
            return None

        parsed.setdefault("args", {})
        if not isinstance(parsed["args"], dict):
            parsed["args"] = {}
        return parsed

    # =========================================================================
    # OBSERVATIONS ET CONDITIONS D'ARRÊT
    # =========================================================================

    @staticmethod
    def _format_metadata(metadata: Optional[Dict]) -> str:
        """Rend les métadonnées les plus discriminantes d'un chunk.

        L'ordre suit OBSERVATION_METADATA_PRIORITY pour que les champs qui
        distinguent réellement deux documents (ticket_id, product, version)
        passent avant ceux qui sont constants sur une instance (client).
        """
        if not metadata:
            return ""
        known = [
            k for k in OBSERVATION_METADATA_PRIORITY
            if metadata.get(k) not in (None, "")
        ]
        extra = sorted(
            k for k in metadata
            if k not in OBSERVATION_METADATA_PRIORITY
            and metadata.get(k) not in (None, "")
        )
        keys = (known + extra)[:OBSERVATION_METADATA_MAX]
        return " | ".join(f"{k}={metadata[k]}" for k in keys)

    @staticmethod
    def _truncate(text: Any, limit: int) -> str:
        """Tronque un texte a `limit` caracteres, avec ellipse si coupe."""
        text = str(text or "")
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "..."

    @staticmethod
    def _format_excerpt(result: Dict) -> str:
        """Extrait compact, sur une seule ligne, du contenu d'un chunk."""
        raw = (
            result.get("excerpt")
            or result.get("text")
            or result.get("content")
            or ""
        )
        collapsed = re.sub(r"\s+", " ", str(raw)).strip()
        return AgentOrchestrator._truncate(collapsed, OBSERVATION_EXCERPT_CHARS)

    @staticmethod
    def _format_observation(tool_name: str, tool_result: Any) -> str:
        """Résume le résultat d'un tool pour le prompt de l'étape suivante.

        Les extraits sont réinjectés : le planificateur doit pouvoir lire le
        contenu retrouvé, faute de quoi il ne dispose que de noms de fichiers
        et invente ce qu'ils contiennent. Le volume reste borné par
        OBSERVATION_MAX_RESULTS et OBSERVATION_EXCERPT_CHARS.
        """
        if isinstance(tool_result, dict) and "count" in tool_result:
            count = tool_result["count"]
            if count == 0:
                note = tool_result.get("note")
                base = f"{tool_name}: aucun résultat trouvé."
                return f"{base} {note}" if note else base

            results = tool_result.get("results") or []
            shown = results[:OBSERVATION_MAX_RESULTS]
            lines = []
            for r in shown:
                header = r.get("file_name") or r.get("file_path") or "?"
                meta = AgentOrchestrator._format_metadata(r.get("metadata"))
                if meta:
                    header = f"{header} [{meta}]"
                excerpt = AgentOrchestrator._format_excerpt(r)
                lines.append(f"- {header}\n  {excerpt}" if excerpt else f"- {header}")

            hidden = count - len(shown)
            suffix = f", {hidden} non affiché(s)" if hidden > 0 else ""
            body = "\n".join(lines)
            return f"{tool_name}: {count} résultat(s){suffix}.\n{body}"
        if isinstance(tool_result, dict) and "escalated" in tool_result:
            return f"escalate_to_human: escalade enregistrée ({tool_result.get('reason')})."
        return f"{tool_name}: {str(tool_result)[:300]}"

    @staticmethod
    def _meets_stop_conditions(final_result: Dict, stop_conditions: Dict) -> bool:
        """
        Vérifie qu'une réponse finale respecte les conditions d'arrêt du
        profil (nombre minimum de sources, citation obligatoire).
        """
        min_sources = stop_conditions.get("min_sources", 0)
        require_citation = stop_conditions.get("require_citation", False)

        sources = final_result.get("sources") or []
        if len(sources) < min_sources:
            return False
        if require_citation and min_sources > 0 and not sources:
            return False
        return True

    # =========================================================================
    # CONSTRUCTION DU RÉSULTAT
    # =========================================================================

    @staticmethod
    def _escalations_in_trace(trace: List[Dict]) -> List[Dict]:
        """
        Escalades effectivement enregistrees pendant CETTE execution.

        Le ToolRegistry est mis en cache par index dans l'API : ses listes
        internes accumulent les appels de toutes les requetes servies par le
        processus. Les interroger ici marquait escalated=True sur toutes les
        reponses suivant la premiere escalade, y compris les reponses saines.
        La trace, elle, est locale au run et sure vis-a-vis des requetes
        concurrentes qui partagent le meme registre.

        Une escalade refusee par le profil ou mise en echec ne compte pas :
        rien n'a ete enregistre, le drapeau doit rester faux.
        """
        found = []
        for entry in trace:
            if entry.get("tool") != "escalate_to_human":
                continue
            result = entry.get("result")
            if isinstance(result, dict) and result.get("escalated"):
                found.append(result)
        return found

    def _finalize(
        self,
        trace: List[Dict],
        response_text: str,
        sources: List[str],
        steps_used: int,
        stopped_reason: str,
    ) -> Dict:
        escalations = self._escalations_in_trace(trace)
        return {
            "result_type": "agentic",
            "response": response_text,
            "sources": sources,
            "escalated": len(escalations) > 0,
            "escalation_reason": escalations[0]["reason"] if escalations else None,
            "trace": trace,
            "steps_used": steps_used,
            "stopped_reason": stopped_reason,
            "query_rewritten": getattr(self, "_last_was_rewritten", False),
            "query_type": getattr(self, "_last_query_type", "general"),
        }

    def _error_result(self, trace: List[Dict], message: str, steps_used: int) -> Dict:
        return {
            "result_type": "agentic",
            "response": f"Une erreur est survenue pendant le traitement agentique: {message}",
            "sources": [],
            "escalated": False,
            "escalation_reason": None,
            "trace": trace,
            "steps_used": steps_used,
            "stopped_reason": "error",
            "query_rewritten": getattr(self, "_last_was_rewritten", False),
            "query_type": getattr(self, "_last_query_type", "general"),
        }
