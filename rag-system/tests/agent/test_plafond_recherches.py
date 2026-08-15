"""Plafond dur du nombre de requetes de recherche par question.

Pourquoi un plafond dans le code et non dans le prompt : mesure sur le jeu
MRAe de vingt cas. Le profil wpd_mrae porte la consigne explicite "tu ne
dois jamais emettre plus de trois requetes de recherche au total". Le
modele en emet quatre, et la quatrieme refond systematiquement les themes
en une requete large. Sur mrae-06, cette requete large
("elements du projet eolien qui assurent l'harmonie avec les parcs
voisins") a ramene le tome 1 volet projet et fait citer ce tome a la place
du tome 5 paysage, degradant un cas qui etait bon dans les deux
configurations precedentes.

Le garde-fou des recherches repetees de #15 ne l'attrape pas : cette
quatrieme requete est reellement nouvelle, elle n'est pas un doublon.

Les tests portent sur run() de bout en bout et non sur une methode isolee,
parce que le precedent de #24 est un drapeau parfaitement teste unitairement
et pourtant inerte dans la boucle reelle.
"""

import json
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RACINE))

from src.agent.orchestrator import AgentOrchestrator  # noqa: E402


class FauxLLM:
    """Rejoue une liste de decisions, puis conclut par final_answer."""

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.appels = 0

    def call_llm(self, system_prompt, plan_prompt):
        self.appels += 1
        self.dernier_prompt = plan_prompt
        if self.decisions:
            return json.dumps(self.decisions.pop(0))
        return json.dumps({
            "tool": "final_answer",
            "args": {"answer": "Reponse etayee.", "sources": ["tome_5.pdf"]},
        })


class FauxRegistre:
    """Double du ToolRegistry reduit aux quatre methodes appelees par run()."""

    OUTILS = ("search_documents", "search_multi", "get_document",
              "final_answer", "no_answer")

    def __init__(self):
        self.appels = []

    def reset_run_state(self):
        pass

    def available_tools(self, allowed=None):
        return {n: {"description": n, "args_schema": {}} for n in self.OUTILS
                if allowed is None or n in allowed}

    def get_seen_results(self):
        return []

    def call(self, tool_name, tool_args, allowed=None):
        self.appels.append((tool_name, tool_args))
        if tool_name == "search_documents":
            return {"results": [{
                "file_name": "tome_5.pdf",
                "score": 0.8,
                "text": "Extrait paysager.",
                "metadata": {},
            }]}
        if tool_name == "search_multi":
            return {"results_by_query": {}}
        if tool_name == "final_answer":
            return {
                "answer": tool_args.get("answer", ""),
                "sources": tool_args.get("sources", []),
            }
        return {}

    @property
    def recherches(self):
        return [a for a in self.appels
                if a[0] in ("search_documents", "search_multi")]


def profil(**extra):
    base = {
        "name": "test_plafond",
        "max_steps": 8,
        "tools_allowed": ["search_documents", "search_multi",
                          "get_document", "no_answer", "final_answer"],
        "system_prompt": "Profil de test.",
        "stop_conditions": {"min_sources": 1, "require_citation": True},
    }
    base.update(extra)
    return base


def recherche(texte):
    return {"tool": "search_documents", "args": {"query": texte}}


# Quatre requetes distinctes, la quatrieme etant la requete de refonte
# observee en production sur mrae-06.
QUATRE_DISTINCTES = [
    recherche("analyse paysagere realisee pour le projet"),
    recherche("integration harmonieuse avec les parcs voisins"),
    recherche("mesures pour la lisibilite d'ensemble des parcs"),
    recherche("elements du projet eolien qui assurent l'harmonie et la "
              "lisibilite d'ensemble"),
]


class TestPlafondEffectif:

    def test_sans_plafond_les_quatre_passent(self):
        """Etat de reference : c'est le comportement mesure aujourd'hui."""
        registre = FauxRegistre()
        orch = AgentOrchestrator(registre, FauxLLM(QUATRE_DISTINCTES))
        orch.run("remarque", profil())
        assert len(registre.recherches) == 4

    def test_plafond_trois_bloque_la_quatrieme(self):
        registre = FauxRegistre()
        orch = AgentOrchestrator(registre, FauxLLM(QUATRE_DISTINCTES))
        orch.run("remarque", profil(max_searches=3))
        assert len(registre.recherches) == 3
        envoyees = [a[1]["query"] for a in registre.recherches]
        assert "elements du projet eolien" not in " ".join(envoyees)

    def test_le_blocage_est_trace(self):
        """Une campagne doit pouvoir compter les blocages sans deviner."""
        registre = FauxRegistre()
        orch = AgentOrchestrator(registre, FauxLLM(QUATRE_DISTINCTES))
        res = orch.run("remarque", profil(max_searches=3))
        notes = [e.get("note") for e in res["trace"]]
        assert notes.count("plafond_recherches_atteint") == 1

    def test_observation_dit_quoi_faire(self):
        """Bloquer sans instruction ferait tourner la boucle jusqu'a
        max_steps, ce qui coute huit appels LLM pour rien."""
        registre = FauxRegistre()
        llm = FauxLLM(QUATRE_DISTINCTES)
        orch = AgentOrchestrator(registre, llm)
        orch.run("remarque", profil(max_searches=3))
        assert "no_answer" in llm.dernier_prompt
        assert "Plafond atteint" in llm.dernier_prompt

    def test_la_boucle_aboutit_quand_meme(self):
        registre = FauxRegistre()
        orch = AgentOrchestrator(registre, FauxLLM(QUATRE_DISTINCTES))
        res = orch.run("remarque", profil(max_searches=3))
        assert res["stopped_reason"] == "final_answer"

    def test_plafond_un(self):
        registre = FauxRegistre()
        orch = AgentOrchestrator(registre, FauxLLM(QUATRE_DISTINCTES))
        orch.run("remarque", profil(max_searches=1))
        assert len(registre.recherches) == 1

    def test_get_document_non_plafonne(self):
        """Le plafond porte sur les requetes envoyees au moteur, pas sur la
        lecture d'un document deja identifie : le profil wpd_mrae demande
        explicitement get_document quand un extrait est trop court."""
        registre = FauxRegistre()
        decisions = QUATRE_DISTINCTES[:3] + [
            {"tool": "get_document", "args": {"file_name": "tome_5.pdf"}},
        ]
        orch = AgentOrchestrator(registre, FauxLLM(decisions))
        orch.run("remarque", profil(max_searches=3))
        assert any(a[0] == "get_document" for a in registre.appels)


class TestValeursAberrantes:
    """Une faute de frappe dans un profil ne doit ni planter le service ni
    interdire silencieusement toute recherche."""

    def test_absence_de_cle(self):
        assert AgentOrchestrator._lire_plafond_recherches(profil()) is None

    def test_none_explicite(self):
        assert AgentOrchestrator._lire_plafond_recherches(
            profil(max_searches=None)) is None

    def test_chaine_non_numerique(self):
        assert AgentOrchestrator._lire_plafond_recherches(
            profil(max_searches="trois")) is None

    def test_chaine_numerique_acceptee(self):
        assert AgentOrchestrator._lire_plafond_recherches(
            profil(max_searches="3")) == 3

    def test_zero_ignore(self):
        assert AgentOrchestrator._lire_plafond_recherches(
            profil(max_searches=0)) is None

    def test_negatif_ignore(self):
        assert AgentOrchestrator._lire_plafond_recherches(
            profil(max_searches=-2)) is None

    def test_zero_laisse_chercher(self):
        """Verification de bout en bout du cas ignore : un plafond a zero
        doit se comporter comme une absence de plafond."""
        registre = FauxRegistre()
        orch = AgentOrchestrator(registre, FauxLLM(QUATRE_DISTINCTES))
        orch.run("remarque", profil(max_searches=0))
        assert len(registre.recherches) == 4


class TestCoexistenceAvecLeGardeFouDeDoublons:
    """#15 bloque les doublons, celui-ci borne les requetes distinctes. Un
    doublon bloque ne doit pas consommer de budget : il n'a jamais atteint
    le moteur."""

    def test_doublon_ne_consomme_pas_le_budget(self):
        registre = FauxRegistre()
        decisions = [
            recherche("analyse paysagere realisee pour le projet"),
            recherche("Analyse paysagere realisee pour le projet ?"),
            recherche("integration harmonieuse avec les parcs voisins"),
            recherche("mesures pour la lisibilite d'ensemble des parcs"),
        ]
        orch = AgentOrchestrator(registre, FauxLLM(decisions))
        res = orch.run("remarque", profil(max_searches=3))
        assert len(registre.recherches) == 3
        notes = [e.get("note") for e in res["trace"]]
        assert notes.count("recherche_repetee_bloquee") == 1
        assert notes.count("plafond_recherches_atteint") == 0
