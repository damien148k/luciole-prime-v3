"""Catalogue de titres dans l'analyse de couverture (query2 v2.9).

Régression du 15 août 2026 sur la Panière du Fort : « quels sont les
enjeux paysagers ? » restitué uniquement depuis le RNT (tome de
synthèse), sans le « 5 - ... Volet paysager et patrimonial.pdf ».
L'analyse de couverture, aveugle à l'inventaire du corpus, a rendu un
verdict d'apparence complète et n'a jamais déclenché la seconde passe.

La v2.9 injecte les titres du corpus dans le prompt de couverture avec
une règle d'inventaire : un tome pertinent absent des passages force un
verdict PARTIEL, même si la synthèse paraît suffisante.
"""

import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RACINE))

from src.agent.catalogue import CatalogueDocuments  # noqa: E402
from src.agent.iterative import (  # noqa: E402
    CATALOGUE_PROMPT_MAX_TITRES,
    COVERAGE_USER_TEMPLATE,
    COVERAGE_USER_TEMPLATE_CATALOGUE,
    IterativePipeline,
    _formater_titres,
)

TITRES_CORPS = [
    "1 - PE de la Panière du Fort - Volet projet.pdf",
    "3 - PE de la Panière du Fort - Volet environnement naturel.pdf",
    "5 - PE de la Panière du Fort - Volet paysager et patrimonial.pdf",
    "PE de la Panière du Fort - RNT Etude d'impact.pdf",
]


class _ClientFactice:
    """Réponse d'agrégation OpenSearch minimale."""

    def __init__(self, buckets=None, leve=False):
        self._buckets = buckets if buckets is not None else list(TITRES_CORPS)
        self._leve = leve
        self.appels = 0

    def search(self, index, body):
        self.appels += 1
        if self._leve:
            raise RuntimeError("OpenSearch indisponible")
        assert body["size"] == 0
        return {
            "aggregations": {
                "titres": {
                    "buckets": [{"key": t, "doc_count": 12} for t in self._buckets]
                }
            }
        }


class _BM25Factice:
    def __init__(self, client):
        self.client = client
        self.index_name = "documents_bm25"


class _HybrideFactice:
    def __init__(self, bm25):
        self.bm25_search = bm25


class _LLMFactice:
    """Enregistre les prompts, rend un verdict piloté."""

    def __init__(self, verdict="COUVERT"):
        self.reponses = [
            '{"verdict": "%s", "manques": [], "requetes": []}' % verdict
        ]
        self.prompts = []

    def call_llm(self, system, prompt):
        self.prompts.append(prompt)
        return self.reponses.pop(0)


class _AnalyzerFactice:
    def __init__(self, bm25, llm):
        self.hybrid_search = _HybrideFactice(bm25)
        self.llm_generator = llm


def _pipeline(analyzer, catalogue=None):
    pipe = IterativePipeline(analyzer, catalogue=catalogue)
    return pipe


# ---------------------------------------------------------------------
# CatalogueDocuments
# ---------------------------------------------------------------------

def test_titres_tries_et_deduits_des_buckets():
    client = _ClientFactice(buckets=list(reversed(TITRES_CORPS)))
    catalogue = CatalogueDocuments(_BM25Factice(client))
    titres = catalogue.titres()
    assert titres == sorted(TITRES_CORPS)
    assert client.appels == 1


def test_cache_ttl_evite_les_relectures():
    client = _ClientFactice()
    catalogue = CatalogueDocuments(_BM25Factice(client), ttl_seconds=600)
    assert catalogue.titres() == sorted(TITRES_CORPS)
    assert catalogue.titres() == sorted(TITRES_CORPS)
    assert client.appels == 1


def test_cache_expire_relance_la_lecture():
    client = _ClientFactice()
    catalogue = CatalogueDocuments(_BM25Factice(client), ttl_seconds=0)
    catalogue.titres()
    catalogue.titres()
    assert client.appels == 2


def test_echec_lecture_sert_le_dernier_cache():
    client = _ClientFactice()
    catalogue = CatalogueDocuments(_BM25Factice(client), ttl_seconds=0)
    premier = catalogue.titres()
    client._leve = True
    assert catalogue.titres() == premier  # repli sur le cache


def test_echec_lecture_sans_cache_retourne_vide():
    catalogue = CatalogueDocuments(_BM25Factice(_ClientFactice(leve=True)))
    assert catalogue.titres() == []


def test_sans_bm25_retourne_vide():
    assert CatalogueDocuments(None).titres() == []


# ---------------------------------------------------------------------
# Formatage du prompt
# ---------------------------------------------------------------------

def test_formater_titres_a_puces():
    rendu = _formater_titres(["b.pdf", "a.pdf"])
    assert rendu == "- b.pdf\n- a.pdf"


def test_formater_titres_plafonne_avec_mention_du_reliquat():
    titres = [f"tome {i:03d}.pdf" for i in range(CATALOGUE_PROMPT_MAX_TITRES + 7)]
    rendu = _formater_titres(titres)
    assert rendu.count("\n- ") == CATALOGUE_PROMPT_MAX_TITRES
    assert "7 autres documents" in rendu


def test_variante_catalogue_conserve_la_spec_json():
    """Le corps de la consigne et les clés JSON restent ceux de la v2.8."""
    assert COVERAGE_USER_TEMPLATE_CATALOGUE != COVERAGE_USER_TEMPLATE
    for marqueur in ('"verdict"', '"manques"', '"requetes"',
                     "COUVERT", "PARTIEL", "NON_COUVERT", "{query}",
                     "{passages}", "{max_q}"):
        assert marqueur in COVERAGE_USER_TEMPLATE_CATALOGUE
    assert "Inventaire des documents du corpus" in (
        COVERAGE_USER_TEMPLATE_CATALOGUE
    )
    assert "{titres}" in COVERAGE_USER_TEMPLATE_CATALOGUE


# ---------------------------------------------------------------------
# Analyse de couverture
# ---------------------------------------------------------------------

def test_prompt_couverture_embarque_l_inventaire():
    llm = _LLMFactice()
    analyzer = _AnalyzerFactice(_BM25Factice(_ClientFactice()), llm)
    pipe = _pipeline(analyzer)
    couverture = pipe._analyse_couverture(
        "quels sont les enjeux paysagers ?",
        [{"text": "Les enjeux sont évalués selon plusieurs niveaux...",
          "file_name": TITRES_CORPS[3], "metadata": {"page_start": 20}}],
    )
    prompt = llm.prompts[0]
    assert "Inventaire des documents du corpus" in prompt
    assert "- 5 - PE de la Panière du Fort - Volet paysager et patrimonial.pdf" in prompt
    assert "Règle d'inventaire" in prompt
    assert couverture["catalogue_titres"] == len(TITRES_CORPS)


def test_verdict_partiel_propage_avec_catalogue():
    """Le tome paysager absent des passages déclenche la seconde passe."""
    llm = _LLMFactice(verdict="PARTIEL")
    llm.reponses = [
        '{"verdict": "PARTIEL", "manques": ["enjeux paysagers"],'
        ' "requetes": ["enjeux paysager patrimonial"]}'
    ]
    analyzer = _AnalyzerFactice(_BM25Factice(_ClientFactice()), llm)
    pipe = _pipeline(analyzer)
    couverture = pipe._analyse_couverture(
        "quels sont les enjeux paysagers ?",
        [{"text": "Synthèse du RNT...", "file_name": TITRES_CORPS[3]}],
    )
    assert couverture["verdict"] == "PARTIEL"
    assert couverture["requetes"] == ["enjeux paysager patrimonial"]


def test_sans_catalogue_le_prompt_est_identique_a_la_v28():
    """Indisponibilité du catalogue = repli strict sur l'ancien prompt."""
    llm = _LLMFactice()
    analyzer = _AnalyzerFactice(_BM25Factice(_ClientFactice(leve=True)), llm)
    pipe = _pipeline(analyzer)
    couverture = pipe._analyse_couverture(
        "quels sont les enjeux paysagers ?",
        [{"text": "Synthèse du RNT...", "file_name": TITRES_CORPS[3]}],
    )
    prompt = llm.prompts[0]
    assert "Inventaire des documents du corpus" not in prompt
    assert couverture["catalogue_titres"] == 0


def test_catalogue_desactive_par_environnement(monkeypatch):
    """QUERY2_CATALOGUE_COUVERTURE=false rend le comportement v2.8."""
    monkeypatch.setenv("QUERY2_CATALOGUE_COUVERTURE", "false")
    # La constante est lue au chargement : on la force pour le test.
    monkeypatch.setattr("src.agent.iterative.CATALOGUE_COUVERTURE_ACTIF", False)
    llm = _LLMFactice()
    analyzer = _AnalyzerFactice(_BM25Factice(_ClientFactice()), llm)
    pipe = _pipeline(analyzer)
    couverture = pipe._analyse_couverture(
        "quels sont les enjeux paysagers ?",
        [{"text": "Synthèse du RNT...", "file_name": TITRES_CORPS[3]}],
    )
    assert "Inventaire" not in llm.prompts[0]
    assert couverture["catalogue_titres"] == 0


def test_echec_llm_conserve_le_repli_couvert_et_la_trace():
    class _LLMPanne(_LLMFactice):
        def call_llm(self, system, prompt):
            raise RuntimeError("LLM indisponible")

    analyzer = _AnalyzerFactice(_BM25Factice(_ClientFactice()), _LLMPanne())
    pipe = _pipeline(analyzer)
    couverture = pipe._analyse_couverture("demande ?", [{"text": "x"}])
    assert couverture["verdict"] == "COUVERT"
    assert couverture["catalogue_titres"] == len(TITRES_CORPS)
