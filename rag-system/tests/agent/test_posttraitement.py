"""Post-traitement des réponses query2 : calculs déterministes + reformulation.

Verrous posés sur le module src/generation/posttraitement.py :

- l'évaluateur n'accepte que l'arithmétique en liste blanche — toute
  construction Python (imports, attributs, chaînes) est rejetée, eval()
  n'existe pas dans ce chemin de code ;
- les nombres à la française (« 12 400,5 », espaces fines comprises)
  sont normalisés avant évaluation ;
- un marqueur en échec reste visible dans le texte et tracé en erreur,
  jamais silencieusement supprimé ;
- la reformulation replie systématiquement sur la réponse d'origine
  (consigne absente, appel en échec, sortie vide ou tronquée) :
  le post-traitement ne peut pas rendre la réponse pire que sans lui.

Lancement : pytest rag-system/tests/agent/test_posttraitement.py
"""

import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RACINE))

from src.generation.posttraitement import (  # noqa: E402
    CADRE_SYSTEME_REFORMULATION,
    ErreurCalcul,
    Reformulateur,
    evaluer_expression,
    formater_nombre,
    resoudre_calculs,
)

import pytest  # noqa: E402


# ---------------------------------------------------------------------------
# Évaluateur arithmétique
# ---------------------------------------------------------------------------

def test_operations_de_base():
    assert evaluer_expression("12.4 / 388 * 100") == pytest.approx(3.19587, rel=1e-4)
    assert evaluer_expression("12400 + 800") == 13200
    assert evaluer_expression("2 * (3 + 4)") == 14
    assert evaluer_expression("-5 + 2") == -3
    assert evaluer_expression("10 % 3") == 1


def test_notation_francaise_hors_fonction():
    # Virgule décimale et séparateurs de milliers (espace, insécable,
    # fine insécable) acceptés hors appel de fonction.
    assert evaluer_expression("12 400,5") == 12400.5
    assert evaluer_expression("12 400") == 12400
    assert evaluer_expression("12 400") == 12400
    assert evaluer_expression("3,5 * 2") == 7.0


def test_fonctions_autorisees():
    assert evaluer_expression("arrondi(3.14159, 2)") == 3.14
    assert evaluer_expression("round(3.6)") == 4
    assert evaluer_expression("pourcentage(12.4, 388)") == pytest.approx(3.19587, rel=1e-4)
    assert evaluer_expression("pct(1, 4)") == 25
    assert evaluer_expression("max(3, 7)") == 7
    assert evaluer_expression("abs(-4)") == 4


def test_virgule_dans_fonction_est_separateur():
    # Dans un appel, la virgule sépare les arguments — la décimale
    # française n'y est pas convertie (consigne : point décimal).
    assert evaluer_expression("arrondi(2.5, 0)") == 2


def test_rejets_securite():
    with pytest.raises(ErreurCalcul):
        evaluer_expression("__import__('os').system('id')")
    with pytest.raises(ErreurCalcul):
        evaluer_expression("open('/etc/passwd').read()")
    with pytest.raises(ErreurCalcul):
        evaluer_expression("'chaine'")
    with pytest.raises(ErreurCalcul):
        evaluer_expression("x + 1")               # nom inconnu
    with pytest.raises(ErreurCalcul):
        evaluer_expression("1 if True else 2")    # construction hors liste
    with pytest.raises(ErreurCalcul):
        evaluer_expression("[1, 2][0]")           # liste / indexation


def test_rejets_arithmetiques():
    with pytest.raises(ErreurCalcul):
        evaluer_expression("1 / 0")
    with pytest.raises(ErreurCalcul):
        evaluer_expression("pourcentage(1, 0)")
    with pytest.raises(ErreurCalcul):
        evaluer_expression("10 ** 100")           # exposant plafonné
    with pytest.raises(ErreurCalcul):
        evaluer_expression("")
    with pytest.raises(ErreurCalcul):
        evaluer_expression("calcule moi ca")


# ---------------------------------------------------------------------------
# Formatage français
# ---------------------------------------------------------------------------

def test_formater_nombre():
    assert formater_nombre(12400) == "12 400"
    assert formater_nombre(12400.0) == "12 400"          # entier déguisé
    assert formater_nombre(3.19587, 1) == "3,2"
    assert formater_nombre(3.19587, 2) == "3,20"
    assert formater_nombre(-1234.56, 1) == "-1 234,6"


# ---------------------------------------------------------------------------
# Résolution des marqueurs
# ---------------------------------------------------------------------------

def test_marqueur_resolu_dans_la_phrase():
    texte = (
        "L'emprise représente {{calc: 12.4 / 388 * 100}} % de la surface "
        "communale (Volet projet, p. 45)."
    )
    resolu, calculs = resoudre_calculs(texte)
    assert "{{calc" not in resolu
    assert "3,2 % de la surface" in resolu
    assert calculs == [{
        "expression": "12.4 / 388 * 100",
        "resultat": "3,2",
        "statut": "ok",
    }]


def test_plusieurs_marqueurs_et_variants_syntaxe():
    texte = "Total {{ calc : 12400 + 800 }} ha, soit {{CALC:pct(13200, 388000)}} %."
    resolu, calculs = resoudre_calculs(texte)
    assert "13 200 ha" in resolu
    assert "3,4 %" in resolu
    assert len(calculs) == 2
    assert all(c["statut"] == "ok" for c in calculs)


def test_marqueur_en_echec_reste_visible_et_trace():
    texte = "Soit {{calc: 12.4 / 0 * 100}} % de la surface."
    resolu, calculs = resoudre_calculs(texte)
    assert "{{calc: 12.4 / 0 * 100}}" in resolu
    assert calculs[0]["statut"] == "erreur"
    assert "zéro" in calculs[0]["detail"]


def test_texte_sans_marqueur_inchange():
    texte = "Réponse ordinaire, sans calcul."
    resolu, calculs = resoudre_calculs(texte)
    assert resolu == texte
    assert calculs == []


# ---------------------------------------------------------------------------
# Reformulateur — faux LLM injecté
# ---------------------------------------------------------------------------

class _LLMFactice:
    """call_llm programmable : retourne une valeur ou lève une exception."""

    def __init__(self, retour="texte reformulé", exception=None):
        self.retour = retour
        self.exception = exception
        self.appels = []

    def call_llm(self, system_prompt: str, prompt: str) -> str:
        self.appels.append((system_prompt, prompt))
        if self.exception:
            raise self.exception
        return self.retour


def _consigne_tmp(tmp_path: Path) -> str:
    fichier = tmp_path / "reformulation.md"
    fichier.write_text(
        "# Documentation d'en-tête, jamais envoyée au modèle.\n"
        "\n---\n\n"
        "Reformule en style administratif.\n",
        encoding="utf-8",
    )
    return str(fichier)


def test_reformulation_appliquee_apres_calculs(tmp_path):
    llm = _LLMFactice()
    reform = Reformulateur(llm, {
        "fichier_prompt": _consigne_tmp(tmp_path),
        "decimales": 1,
    })
    texte = "L'emprise fait {{calc: 12.4 / 388 * 100}} % de la commune."
    final, trace = reform.traiter(texte)

    assert final == "texte reformulé"
    assert trace["recriture"]["effectuee"] is True
    assert trace["calculs"][0]["resultat"] == "3,2"
    # Le LLM reçoit le texte déjà résolu (jamais le marqueur) et le cadre
    # de fidélité en système.
    system, prompt = llm.appels[0]
    assert "{{calc" not in prompt
    assert "3,2 %" in prompt
    assert system == CADRE_SYSTEME_REFORMULATION
    # L'en-tête documentaire du fichier n'atteint pas le modèle.
    assert "Documentation d'en-tête" not in prompt
    assert "Reformule en style administratif." in prompt


def test_recriture_desactivee_resout_quand_meme_les_calculs(tmp_path):
    llm = _LLMFactice()
    reform = Reformulateur(llm, {
        "fichier_prompt": _consigne_tmp(tmp_path),
        "recriture": False,
    })
    final, trace = reform.traiter("Soit {{calc: 1 / 4 * 100}} %.")
    assert final == "Soit 25 %."
    assert trace["recriture"] == {"effectuee": False, "motif": "desactivee"}
    assert llm.appels == []


def test_consigne_absente_replie_sans_appel():
    llm = _LLMFactice()
    reform = Reformulateur(llm, {"fichier_prompt": "nulle/part/consigne.md"})
    texte = "Réponse brute avec {{calc: 2 * 3}} unités."
    final, trace = reform.traiter(texte)
    assert final == "Réponse brute avec 6 unités."   # calculs résolus
    assert trace["recriture"]["motif"] == "consigne_absente"
    assert llm.appels == []


def test_echec_appel_replie_sur_originale(tmp_path):
    llm = _LLMFactice(exception=RuntimeError("backend injoignable"))
    reform = Reformulateur(llm, {"fichier_prompt": _consigne_tmp(tmp_path)})
    texte = "Réponse brute suffisamment longue pour passer les gardes."
    final, trace = reform.traiter(texte)
    assert final == texte
    assert trace["recriture"]["motif"] == "erreur_appel"


def test_sortie_vide_replie(tmp_path):
    llm = _LLMFactice(retour="   ")
    reform = Reformulateur(llm, {"fichier_prompt": _consigne_tmp(tmp_path)})
    texte = "Réponse brute suffisamment longue pour passer les gardes."
    final, trace = reform.traiter(texte)
    assert final == texte
    assert trace["recriture"]["motif"] == "vide"


def test_garde_anti_troncature(tmp_path):
    llm = _LLMFactice(retour="Trop court.")
    reform = Reformulateur(llm, {"fichier_prompt": _consigne_tmp(tmp_path)})
    texte = "Réponse brute longue, " * 20
    final, trace = reform.traiter(texte)
    assert final == texte
    assert trace["recriture"]["motif"] == "tronquee"


def test_reponse_vide_passee_telle_quelle():
    reform = Reformulateur(_LLMFactice(), {})
    final, trace = reform.traiter("")
    assert final == ""
    assert trace["recriture"]["motif"] == "reponse_vide"
