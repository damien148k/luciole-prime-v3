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


class AgentOrchestrator:
    """
    Exécute une boucle agentique bornée pour répondre à une requête
    utilisateur, en s'appuyant sur un profil métier (tools autorisés,
    prompt système, conditions d'arrêt) et un ToolRegistry partagé.
    """

    def __init__(self, tool_registry: ToolRegistry, llm_generator):
        """
        Args:
            tool_registry: instance de ToolRegistry (tools.py)
            llm_generator: instance de LLMGenerator (generation/llm.py),
                utilisée uniquement via call_llm() pour la planification
                (pas generate(), qui est le chemin RAG procédural existant)
        """
        self.tools = tool_registry
        self.llm = llm_generator

    def run(
        self,
        query: str,
        profile: Dict,
        history: Optional[List[Dict]] = None,
    ) -> Dict:
        """
        Exécute la boucle agentique pour une requête donnée.

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
        if "final_answer" not in available:
            # Sans final_answer le LLM n'a aucun moyen propre de terminer
            # la boucle : on le rend toujours disponible par sécurité.
            available = dict(available, **self.tools.available_tools(allowed=["final_answer"]))
            tools_allowed = list(tools_allowed) + ["final_answer"]

        trace: List[Dict] = []
        observations: List[str] = []
        rejected_final_answers: List[str] = []

        for step in range(1, max_steps + 1):
            plan_prompt = self._build_plan_prompt(
                query=query,
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
                        "escalade si tu ne trouves vraiment aucune source."
                    )
                    continue

                return self._finalize(
                    trace, result.get("text", ""), result.get("sources", []),
                    step, "final_answer"
                )

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

    def _build_plan_prompt(
        self,
        query: str,
        tools_description: Dict[str, Dict],
        observations: List[str],
        history: Optional[List[Dict]],
        step: int,
        max_steps: int,
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

        return f"""{history_block}Question de l'utilisateur: {query}

Outils disponibles:
{tools_block}

{observations_block}Étape {step}/{max_steps}.

Choisis UN SEUL outil à appeler maintenant, en fonction de ce que tu sais déjà.
Si tu as assez d'informations pour répondre, appelle final_answer.

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

    def _finalize(
        self,
        trace: List[Dict],
        response_text: str,
        sources: List[str],
        steps_used: int,
        stopped_reason: str,
    ) -> Dict:
        escalations = self.tools.get_escalations()
        return {
            "result_type": "agentic",
            "response": response_text,
            "sources": sources,
            "escalated": len(escalations) > 0,
            "escalation_reason": escalations[0]["reason"] if escalations else None,
            "trace": trace,
            "steps_used": steps_used,
            "stopped_reason": stopped_reason,
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
        }
