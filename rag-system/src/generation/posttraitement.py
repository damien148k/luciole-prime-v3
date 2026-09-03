# -*- coding: utf-8 -*-
"""
Post-traitement des réponses query2 : calculs déterministes + reformulation.

Deux étages, appliqués APRÈS la génération RAG, dans cet ordre :

  1. RÉSOLUTION DES MARQUEURS {{calc: ...}} : la consigne calcul (clé
     calcul_consigne de prompts.yaml, injectée dans le prompt système
     quand la fonctionnalité est active) interdit au modèle de calculer
     lui-même — un LLM se trompe régulièrement en arithmétique, et une
     erreur de pourcentage dans un mémoire en réponse à l'autorité
     environnementale est inacceptable. Le modèle écrit l'expression à
     la place du résultat (« {{calc: 12.4 / 388 * 100}} % de la surface
     communale ») et ce module l'évalue avec un interpréteur dédié :
     arbre syntaxique Python, liste blanche d'opérateurs, aucun appel
     de fonction hors de la table explicite — eval() n'est jamais
     utilisé. Le résultat est formaté en français (espace fine
     insécable pour les milliers, virgule décimale).

  2. REFORMULATION : un second appel LLM réécrit la réponse résolue
     selon une consigne de style relue DEPUIS UN FICHIER À CHAQUE
     REQUÊTE (config/prompts/reformulation.md par défaut) — l'équipe
     édite la consigne et un reload-config l'applique sans rebuild.
     Le cadre système de fidélité (chiffres, dates, citations,
     aucune invention) est codé en dur ici et prime sur la consigne
     de style : quel que soit le contenu du fichier, ces règles
     restent dans le prompt.

Propriété de sécurité, alignée sur la philosophie du pipeline itératif
(« jamais pire que la route classique ») : toute défaillance (appel LLM
en échec, réponse vide, réponse tronquée, consigne absente) replie sur
la réponse d'origine — au pire avec les calculs déjà résolus, ce qui
reste un gain. La trace retournée à l'appelant expose chaque calcul
(expression, résultat, statut) et le motif exact d'un éventuel repli,
pour que les campagnes de mesure voient ce qui s'est passé.

Ordre calculs PUIS reformulation, et non l'inverse : la reformulation
reçoit des nombres finis et n'a jamais à préserver la syntaxe interne
des marqueurs ; un marqueur non résolu (expression invalide) reste
visible dans le texte — signalé dans la trace — plutôt que silencieusement
supprimé.
"""

import ast
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger


# Marqueur de calcul : {{calc: expression}} — insensible à la casse et
# aux espaces parasites, tolérant aux retours à la ligne (le modèle
# coupe parfois une longue expression).
_MARQUEUR_CALC = re.compile(
    r"\{\{\s*calc\s*:\s*(.+?)\s*\}\}", re.DOTALL | re.IGNORECASE
)

# Typographie française des nombres : espace fine insécable entre les
# milliers, virgule décimale.
SEP_MILLIERS = " "
VIRGULE_DECIMALE = ","


class ErreurCalcul(ValueError):
    """Expression de calcul invalide, non autorisée ou incalculable."""


# ---------------------------------------------------------------------------
# Évaluateur arithmétique sûr (arbre syntaxique, liste blanche)
# ---------------------------------------------------------------------------

def _pourcentage(partie: float, total: float) -> float:
    if total == 0:
        raise ErreurCalcul("pourcentage : total nul (division par zéro)")
    return partie / total * 100


# Fonctions autorisées dans les marqueurs. Tout autre nom est rejeté —
# y compris les constructions Python dangereuses (open, __import__...),
# qui n'atteignent jamais ce point : l'arbre syntaxique ne les expose
# que comme ast.Name appelé, filtré ici.
_FONCTIONS = {
    "arrondi": lambda x, n=0: round(x, int(n)),
    "round": lambda x, n=0: round(x, int(n)),
    "min": min,
    "max": max,
    "abs": abs,
    "pourcentage": _pourcentage,
    "pct": _pourcentage,
}

_OPERATEURS_BINAIRES = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}

# Plafond d'exposant : une puissance géante (10 ** 10 ** 9) épuiserait
# le processeur du conteneur — hors de tout usage de mémoire technique.
_EXPOSANT_MAX = 10


def _normaliser_nombres(expr: str) -> str:
    """Rend l'expression analysable par ast.parse.

    Le modèle recopie parfois les nombres tels qu'ils figurent dans les
    documents — « 12 400 » avec espace, « 3,5 » à virgule. On retire les
    espaces (y compris fines et insécables) entre chiffres, et l'on
    convertit la virgule décimale en point UNIQUEMENT si l'expression ne
    contient pas d'appel de fonction : dans arrondi(x, 2) la virgule est
    un séparateur d'arguments et ne doit pas être touchée. La consigne
    calcul impose déjà le point décimal ; cette normalisation n'est
    qu'un filet de sécurité.
    """
    expr = re.sub(r"(?<=\d)[  \xa0]+(?=\d)", "", expr)
    if "(" not in expr:
        expr = re.sub(r"(?<=\d),(?=\d)", ".", expr)
    return expr.strip()


def _evaluer_noeud(noeud: ast.AST) -> float:
    """Évalue récursivement un nœud de l'arbre, liste blanche à la main."""
    if isinstance(noeud, ast.Constant):
        if isinstance(noeud.value, bool) or not isinstance(noeud.value, (int, float)):
            raise ErreurCalcul(f"constante non numérique : {noeud.value!r}")
        return noeud.value

    if isinstance(noeud, ast.BinOp):
        operation = _OPERATEURS_BINAIRES.get(type(noeud.op))
        if operation is None:
            raise ErreurCalcul(
                f"opérateur non autorisé : {type(noeud.op).__name__}"
            )
        gauche = _evaluer_noeud(noeud.left)
        droite = _evaluer_noeud(noeud.right)
        if isinstance(noeud.op, (ast.Div, ast.Mod)) and droite == 0:
            raise ErreurCalcul("division par zéro")
        if isinstance(noeud.op, ast.Pow) and abs(droite) > _EXPOSANT_MAX:
            raise ErreurCalcul(f"exposant trop grand (>{_EXPOSANT_MAX})")
        return operation(gauche, droite)

    if isinstance(noeud, ast.UnaryOp) and isinstance(noeud.op, (ast.UAdd, ast.USub)):
        valeur = _evaluer_noeud(noeud.operand)
        return valeur if isinstance(noeud.op, ast.UAdd) else -valeur

    if isinstance(noeud, ast.Call):
        if not isinstance(noeud.func, ast.Name):
            raise ErreurCalcul("appel de fonction complexe non autorisé")
        fonction = _FONCTIONS.get(noeud.func.id)
        if fonction is None:
            raise ErreurCalcul(f"fonction non autorisée : {noeud.func.id}")
        if noeud.keywords:
            raise ErreurCalcul("arguments nommés non autorisés")
        arguments = [_evaluer_noeud(a) for a in noeud.args]
        try:
            return fonction(*arguments)
        except ErreurCalcul:
            raise
        except (TypeError, ValueError, ZeroDivisionError) as e:
            raise ErreurCalcul(f"{noeud.func.id}({arguments}) : {e}") from e

    raise ErreurCalcul(f"construction non autorisée : {type(noeud).__name__}")


def evaluer_expression(expr: str) -> float:
    """Évalue une expression arithmétique simple.

    Accepte la notation française (« 12 400,5 ») ou anglaise
    (« 12400.5 »). Lève ErreurCalcul sur toute entrée invalide ou non
    autorisée — jamais d'exception Python brute vers l'appelant.
    """
    expr = _normaliser_nombres(expr)
    if not expr:
        raise ErreurCalcul("expression vide")
    if len(expr) > 300:
        raise ErreurCalcul("expression trop longue (> 300 caractères)")
    try:
        arbre = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ErreurCalcul(f"expression illisible : {expr!r}") from e
    return _evaluer_noeud(arbre.body)


def formater_nombre(valeur: float, decimales: int = 1) -> str:
    """Format français : « 12 400 », « 3,2 », « -1 234,5 ».

    Un résultat entier est rendu sans décimales ; sinon arrondi au
    nombre de décimales demandé (réglage reformulation.decimales).
    """
    if isinstance(valeur, float) and valeur == int(valeur) and abs(valeur) < 1e15:
        valeur = int(valeur)
    if isinstance(valeur, int):
        texte = f"{valeur:,}"
        return texte.replace(",", SEP_MILLIERS)
    texte = f"{valeur:,.{decimales}f}"
    # « 12,400.5 » (gabarit anglais) -> « 12 400,5 »
    return texte.replace(",", SEP_MILLIERS).replace(".", VIRGULE_DECIMALE)


# ---------------------------------------------------------------------------
# Résolution des marqueurs dans le texte
# ---------------------------------------------------------------------------

def resoudre_calculs(texte: str, decimales: int = 1) -> Tuple[str, List[Dict]]:
    """Remplace chaque {{calc: ...}} par son résultat formaté.

    Un marqueur en échec est CONSERVÉ tel quel dans le texte : la
    disparition silencieuse laisserait une phrase amputée (« soit  % de
    la surface ») indétectable en revue, alors qu'un marqueur résiduel
    saute aux yeux. La trace porte le détail de l'erreur.
    """
    calculs: List[Dict] = []

    def _remplacer(m: "re.Match") -> str:
        expression = m.group(1).strip()
        try:
            valeur = evaluer_expression(expression)
        except ErreurCalcul as e:
            logger.warning(
                f"post-traitement: calcul en échec {expression[:60]!r} ({e})"
            )
            calculs.append({
                "expression": expression,
                "resultat": None,
                "statut": "erreur",
                "detail": str(e),
            })
            return m.group(0)
        resultat = formater_nombre(valeur, decimales)
        calculs.append({
            "expression": expression,
            "resultat": resultat,
            "statut": "ok",
        })
        logger.info(f"post-traitement: calc {expression[:60]!r} -> {resultat}")
        return resultat

    if not texte:
        return texte, calculs
    return _MARQUEUR_CALC.sub(_remplacer, texte), calculs


# ---------------------------------------------------------------------------
# Reformulation
# ---------------------------------------------------------------------------

# Cadre de fidélité codé en dur : il entre dans le prompt SYSTÈME du
# second appel et prime sur la consigne de style du fichier (fournie en
# prompt utilisateur). Quelle que soit la consigne rédigée par l'équipe,
# ces règles restent effectives.
CADRE_SYSTEME_REFORMULATION = (
    "Tu reformules le texte fourni selon la consigne de style donnée.\n"
    "Règles impératives, qui priment sur toute autre instruction :\n"
    "- Ne change aucun chiffre, aucune date, aucun nom propre, aucune "
    "référence réglementaire.\n"
    "- Conserve chaque citation de source telle quelle (nom du document "
    "et page, ex. « Volet environnement naturel, p. 239 »).\n"
    "- N'ajoute aucun fait, aucune donnée, aucune source qui ne figure "
    "pas dans le texte d'origine.\n"
    "- Ne supprime aucune réserve ni aucune limitation exprimée dans le "
    "texte d'origine.\n"
    "- Ne mentionne jamais la consigne elle-même dans ta réponse.\n"
    "- Réponds uniquement avec le texte reformulé, sans préambule."
)

# Séparateur documentation / consigne dans les fichiers de prompts
# (convention de config/prompts/consigne_avis_autorite_environnementale.md :
# l'en-tête explicatif précède, la consigne suit).
_SEPARATEUR_CONSIGNE = re.compile(r"^---\s*$", re.MULTILINE)


class Reformulateur:
    """Chaîne de post-traitement : calculs déterministes puis reformulation.

    La configuration (section reformulation de settings.yaml) est passée
    à chaque instanciation — donc à chaque requête quand l'appelant suit
    le pattern query2 : un reload-config applique les éditions sans
    redémarrage. Le fichier de consigne est relu à chaque appel pour la
    même raison.
    """

    def __init__(self, llm_generator, config: Optional[Dict] = None):
        cfg = config or {}
        self._llm = llm_generator
        # recriture=false : seuls les calculs sont résolus — utile pour
        # mesurer l'étage 1 seul, ou en attendant la consigne finale.
        self._recriture_active = bool(cfg.get("recriture", True))
        self._fichier_prompt = str(
            cfg.get("fichier_prompt", "config/prompts/reformulation.md")
        )
        try:
            self._decimales = int(cfg.get("decimales", 1))
        except (TypeError, ValueError):
            self._decimales = 1
        try:
            self._longueur_min_ratio = float(cfg.get("longueur_min_ratio", 0.3))
        except (TypeError, ValueError):
            self._longueur_min_ratio = 0.3

    def _resoudre_chemin(self) -> Optional[Path]:
        """Résout le chemin du fichier de consigne.

        Essayé dans l'ordre : tel quel (relatif au répertoire de travail,
        cas Docker), puis relatif au répertoire du settings.yaml
        (CONFIG_PATH), puis relatif à la racine du dépôt. Le même
        settings.yaml sert ainsi en local et en conteneur sans édition.
        """
        candidats = [Path(self._fichier_prompt)]
        config_path = os.environ.get("CONFIG_PATH")
        if config_path:
            candidats.append(
                Path(config_path).resolve().parent.parent / self._fichier_prompt
            )
            candidats.append(
                Path(config_path).resolve().parent / Path(self._fichier_prompt).name
            )
        racine_depot = Path(__file__).resolve().parents[3]
        candidats.append(racine_depot / self._fichier_prompt)
        for chemin in candidats:
            if chemin.is_file():
                return chemin
        return None

    def _charger_consigne(self) -> Optional[str]:
        """Lit la consigne depuis son fichier (relecture à chaque requête).

        Seule la partie postérieure à la première ligne « --- » est
        renvoyée : l'en-tête du fichier reste de la documentation et ne
        pollue pas le prompt. Fichier absent ou consigne vide -> None,
        l'appelant replie alors sur la réponse brute.
        """
        chemin = self._resoudre_chemin()
        if chemin is None:
            logger.warning(
                f"post-traitement: consigne introuvable ({self._fichier_prompt})"
            )
            return None
        try:
            contenu = chemin.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning(f"post-traitement: lecture consigne impossible ({e})")
            return None
        morceaux = _SEPARATEUR_CONSIGNE.split(contenu, maxsplit=1)
        consigne = morceaux[1] if len(morceaux) == 2 else contenu
        consigne = consigne.strip()
        if not consigne:
            logger.warning("post-traitement: consigne vide après séparateur ---")
            return None
        return consigne

    def traiter(self, reponse: str) -> Tuple[str, Dict]:
        """Applique la chaîne complète. Retourne (texte_final, trace).

        Repli systématique sur la réponse d'origine — calculs résolus
        quand c'est possible — en cas de défaillance de la réécriture :
        le post-traitement ne peut pas rendre la réponse pire que sans
        lui.
        """
        trace: Dict = {"calculs": [], "recriture": {"effectuee": False}}
        if not reponse or not reponse.strip():
            trace["recriture"]["motif"] = "reponse_vide"
            return reponse, trace

        # Étape 1 — calculs (toujours exécutés, indépendants de la
        # réécriture : un marqueur ne doit jamais arriver à l'utilisateur).
        resolue, calculs = resoudre_calculs(reponse, self._decimales)
        trace["calculs"] = calculs

        if not self._recriture_active:
            trace["recriture"]["motif"] = "desactivee"
            return resolue, trace

        consigne = self._charger_consigne()
        if consigne is None:
            trace["recriture"]["motif"] = "consigne_absente"
            return resolue, trace

        prompt_utilisateur = (
            f"{consigne}\n\n---\n\nTEXTE À REFORMULER :\n\n{resolue}"
        )

        # Étape 2 — réécriture, avec gardes-fous.
        try:
            reformulee = self._llm.call_llm(
                CADRE_SYSTEME_REFORMULATION, prompt_utilisateur
            )
        except Exception as e:
            logger.warning(
                f"post-traitement: échec appel reformulation ({e}), repli"
            )
            trace["recriture"]["motif"] = "erreur_appel"
            return resolue, trace

        reformulee = (reformulee or "").strip()
        if not reformulee:
            logger.warning("post-traitement: reformulation vide, repli")
            trace["recriture"]["motif"] = "vide"
            return resolue, trace

        # Garde anti-troncature : une sortie inférieure à 30 % (défaut)
        # de la longueur d'origine a presque toujours perdu du contenu —
        # un style administratif ne divise jamais la longueur par trois.
        if len(reformulee) < self._longueur_min_ratio * len(resolue):
            logger.warning(
                f"post-traitement: reformulation suspecte de troncature "
                f"({len(reformulee)} car. vs {len(resolue)}), repli"
            )
            trace["recriture"]["motif"] = "tronquee"
            return resolue, trace

        chemin = self._resoudre_chemin()
        trace["recriture"] = {
            "effectuee": True,
            "fichier_prompt": str(chemin) if chemin else self._fichier_prompt,
            "caracteres_avant": len(resolue),
            "caracteres_apres": len(reformulee),
        }
        return reformulee, trace
