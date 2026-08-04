"""Trajet des numeros de page jusqu'aux observations du planificateur.

Trois maillons ont ete corriges successivement sur ce trajet, chacun
masquant le suivant :

1. #25 : le parser lisait une clef de pagination absente du conteneur
2. #26 : la projection des passages de l'API ne recopiait pas page_start
3. celui-ci : `_summarize_results` filtre les metadonnees sur une liste
   blanche codee en dur ou page_start ne figurait pas, en amont de
   `_format_metadata` et donc du drapeau `agent.observation_pages`
   introduit par #24

Consequence mesuree : campagne de vingt cas relancee avec le drapeau
actif, vingt reponses identiques au caractere pres a la campagne menee
sans le drapeau. Le drapeau etait inerte par construction.

Les tests portent sur le contrat entre les deux modules, pas sur chacun
pris isolement, puisque c'est precisement l'articulation qui a lache.
"""

import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RACINE))

from src.agent.tools import ToolRegistry, METADATA_RESUMEES  # noqa: E402
from src.agent.orchestrator import (  # noqa: E402
    AgentOrchestrator,
    configurer_pages_dans_observations,
)

RESULTAT = {
    "file_name": "tome_4.pdf",
    "file_path": "/app/data/mrae/tome_4.pdf",
    "score": 0.81,
    "text": "Le raccordement electrique emprunte la voirie existante.",
    "metadata": {
        "page_start": 224,
        "page_end": 225,
        "client": "wpd",
        "chunk_index": 17,
    },
}


class TestListeBlanche:

    def test_pages_conservees(self):
        assert "page_start" in METADATA_RESUMEES
        assert "page_end" in METADATA_RESUMEES

    def test_resume_conserve_les_pages(self):
        resume = ToolRegistry._summarize_results([RESULTAT])
        assert resume[0]["metadata"]["page_start"] == 224
        assert resume[0]["metadata"]["page_end"] == 225

    def test_clefs_hors_liste_toujours_filtrees(self):
        """La liste blanche reste une liste blanche : l'elargir aux pages
        ne doit pas laisser passer le reste des metadonnees techniques."""
        resume = ToolRegistry._summarize_results([RESULTAT])
        assert "chunk_index" not in resume[0]["metadata"]

    def test_metier_preserve(self):
        resume = ToolRegistry._summarize_results([RESULTAT])
        assert resume[0]["metadata"]["client"] == "wpd"


class TestDrapeauEffectif:
    """Le drapeau doit maintenant commander reellement l'affichage."""

    def teardown_method(self):
        configurer_pages_dans_observations(False)

    def test_actif_les_pages_apparaissent(self):
        configurer_pages_dans_observations(True)
        resume = ToolRegistry._summarize_results([RESULTAT])
        rendu = AgentOrchestrator._format_metadata(resume[0]["metadata"])
        assert "page_start=224" in rendu
        assert "page_end=225" in rendu

    def test_inactif_les_pages_disparaissent(self):
        configurer_pages_dans_observations(False)
        resume = ToolRegistry._summarize_results([RESULTAT])
        rendu = AgentOrchestrator._format_metadata(resume[0]["metadata"])
        assert "page_start" not in rendu
        assert "page_end" not in rendu

    def test_inactif_le_metier_reste(self):
        """La desactivation ne doit retirer que les pages."""
        configurer_pages_dans_observations(False)
        resume = ToolRegistry._summarize_results([RESULTAT])
        rendu = AgentOrchestrator._format_metadata(resume[0]["metadata"])
        assert "client=wpd" in rendu

    def test_actif_les_pages_passent_en_tete(self):
        """Le budget d'affichage etant borne a six clefs, les pages ne
        servent a rien si elles sont reléguees derriere les champs
        metier d'une instance qui en declare beaucoup."""
        configurer_pages_dans_observations(True)
        meta = dict(RESULTAT["metadata"])
        meta.update({"projet": "a", "phase": "b", "thematique": "c",
                     "departement": "d", "product": "e", "version": "f"})
        resume = ToolRegistry._summarize_results([{**RESULTAT, "metadata": meta}])
        rendu = AgentOrchestrator._format_metadata(resume[0]["metadata"])
        assert rendu.startswith("page_start=224 | page_end=225")
