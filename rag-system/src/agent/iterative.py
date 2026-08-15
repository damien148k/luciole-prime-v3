"""
Pipeline itératif v2.9 (endpoint expérimental /api/query2).

Conception : DESIGN_agent_v2_pipeline_iteratif.md (8 août 2026).

Structure fixe en 4 étapes, par opposition à la boucle libre plan/act/observe
de la route agent v1 (mesurée : 11 esquives sur 16 au benchmark MRAe) :

  1. RECHERCHE A + génération : appel identique à la route classique
     (analyzer.analyze mode chat). Si l'étape 2 conclut COUVERT, la réponse
     retournée EST celle de la route classique — la v2 ne peut pas faire
     moins bien que la classique (propriété de sécurité).
  2. ANALYSE DE COUVERTURE : 1 appel LLM qui lit les passages COMPLETS
     (pas des extraits de 400 caractères comme la boucle v1) et répond en
     JSON structuré : verdict, manques, requêtes ciblées. Depuis la v2.9,
     le prompt embarque l'inventaire des titres du corpus (catalogue) :
     un tome pertinent absent des passages force un verdict PARTIEL,
     même si la synthèse paraît complète (mesuré sur la Panière du
     Fort : « enjeux paysagers » restitué sans le volet paysager).
  3. RECHERCHE B (si besoin) : quota réservé + question construite.
     a. QUESTION CONSTRUITE : le LLM extrait le SUJET de la demande
        (3-6 mots, tâche bornée), puis le code assemble la question
        (« Que disent les documents sur {sujet} ? »). Sans cela, le LLM
        commente l'absence de la demande elle-même dans les documents —
        qui ne la contiennent jamais — au lieu de répondre sur le fond
        (mesuré : R11 esquivé malgré les bonnes pages dans les sources).
        La reformulation libre (v2.4) laissait parfois l'auteur de la
        demande devenir le sujet de la question ; l'extraction bornée
        supprime ce mode d'échec.
     b. QUOTA RÉSERVÉ : la fusion multi-query dilue les passages trouvés
        par les requêtes ciblées (mesuré : une page au rang 3 de sa
        requête tombe au rang 55 du pool fusionné, puis est éliminée par
        le reranker qui ne note que contre la demande d'origine). Chaque
        requête ciblée fait donc sa PROPRE recherche + rerank et réserve
        QUOTA_PAR_REQUETE places garanties dans le top final ; le reste
        vient de la recherche générale sur la question reformulée.
  4. GÉNÉRATION finale : prompt RAG classique, température 0 (mêmes
     appels que la route classique : _build_context + generate), avec
     la question reformulée comme « Question : ».

Plafond dur : 1 round itératif par défaut (2 recherches max au total).
L'esquive honnête reste possible si le corpus ne contient pas l'information
(comportement légitime mesuré : radar, Natura 2000).

v2.10 — règle d'inventaire déterministe : pour les interrogations sur
les ENJEUX d'un sujet, le code force PARTIEL quand un tome de
l'inventaire porte le sujet sans qu'aucun passage n'en provienne ; la
requête ciblée est construite par concaténation sujet + mots du titre,
jamais par le modèle. La règle ne voit que les tomes indexés : un
document non ingéré est absent de l'inventaire et lui reste invisible
(cas mesuré le 15 août 2026 sur la Panière du Fort : le volet paysager
manquait à l'index OpenSearch — le verdict COUVERT était alors correct
au vu du corpus réellement indexé)."""

import json
import os
import re
import unicodedata
from typing import Dict, List, Optional

from loguru import logger

from src.agent.catalogue import CatalogueDocuments


# Nombre de passages complets soumis à l'analyse de couverture.
# 12 passages x ~1200 caractères ~ 5k tokens : tient dans la fenêtre 16k
# avec le prompt et la réponse JSON.
COVERAGE_MAX_PASSAGES = 12
COVERAGE_PASSAGE_CHARS = 1200

# Nombre maximum de requêtes ciblées demandées à l'analyse de couverture.
COVERAGE_MAX_QUERIES = 3

# Places garanties dans le top final pour chaque requête ciblée
# (quota de diversité : empêche le sujet dominant d'écraser les manques).
QUOTA_PAR_REQUETE = 5

# Inventaire des titres du corpus dans le prompt de couverture (v2.9).
# Désactivable par QUERY2_CATALOGUE_COUVERTURE=false pour mesurer la
# contribution du catalogue au benchmark, chemin nominal sinon inchangé.
CATALOGUE_COUVERTURE_ACTIF = (
    os.environ.get("QUERY2_CATALOGUE_COUVERTURE", "true").lower() == "true"
)

# Plafond de titres injectés dans le prompt : les 12 passages x 1200
# caractères occupent déjà ~5k tokens sur la fenêtre 16k.
CATALOGUE_PROMPT_MAX_TITRES = 100

SUJET_SYSTEM_PROMPT = (
    "Tu extrais le sujet d'une demande en quelques mots. Tu réponds "
    "uniquement avec le sujet, sans commentaire."
)

SUJET_USER_TEMPLATE = """Quel est le sujet de la demande suivante ?

Règles :
- 3 à 6 mots, avec le déterminant (le, la, les, du, de la, des, l')
- le sujet désigne les informations recherchées, JAMAIS l'auteur de
  la demande (autorité, client, comité...) ni la demande elle-même
- garde la langue de la demande

Exemples :
Demande : Il est recommandé de préciser le calendrier des travaux et les horaires de chantier.
Sujet : le calendrier des travaux et les horaires de chantier

Demande : Le comité demande de compléter le rapport avec une analyse des coûts de maintenance.
Sujet : l'analyse des coûts de maintenance

Demande : {query}
Sujet :"""

# Consigne renforcée pour la seconde tentative : le modèle multilingual
# décroche parfois vers une autre langue sur les demandes longues
# (mesuré sur Beaumont Sud le 9 août 2026) ; le rappel explicite de la
# langue cible corrige ce glitch stochastique dans la plupart des cas.
SUJET_SYSTEM_PROMPT_RENFORCE = (
    SUJET_SYSTEM_PROMPT
    + " Tu réponds exclusivement en français : aucun caractère chinois,"
      " japonais, coréen, ni aucun symbole spécial."
)

# Gabarit de construction de la question à partir du sujet extrait.
# Mécanique volontairement : la forme de la question ne dépend plus du
# modèle, seul le sujet vient du LLM.
QUESTION_TEMPLATE = "Que disent les documents sur {sujet} ?"

# Lettres attendues dans un sujet en français : alphabet de base et
# accents effectivement utilisés (àâäçéèêëîïôöùûüÿæœ et majuscules).
_LETTRES_FR = re.compile(r"[a-zàâäçéèêëîïôöùûüÿæœ]", re.IGNORECASE)

# Déclencheur de la règle d'inventaire déterministe (v2.10) : la demande
# doit interroger les ENJEUX d'un sujet. Le déclencheur est étroit à
# dessein — un mécanisme générique « titre correspond à la demande »
# échouerait sur les noms de projet présents dans tous les tomes.
_MOTIFS_ENJEUX = re.compile(r"\benjeux?\b", re.IGNORECASE)

# Mots vides exclus du matching de sujet et des termes de titre.
_MOTS_VIDES = frozenset({
    "le", "la", "les", "du", "de", "des", "un", "une", "au", "aux",
    "et", "ou", "en", "dans", "sur", "sont", "est", "quels", "quelles",
    "quel", "quelle", "pe", "d", "l", "etude", "etudes",
})

# Termes structurels des noms de tomes : présents dans tous les fichiers
# du corpus (projet, étude, impact...), ils ne discriminent rien et ne
# doivent jamais déclencher ni composer une requête d'inventaire.
_TERMES_STRUCTURANTS = frozenset({
    "projet", "volet", "tome", "rnt", "fort", "panere", "paniere",
    "panier", "impact", "impacts", "environnement",
})


def _normaliser_mot(mot: str) -> str:
    """Minuscules sans accents, apostrophes et tirets normalisés."""
    mot = mot.lower().replace("'", " ").replace("-", " ")
    return "".join(
        c for c in unicodedata.normalize("NFD", mot)
        if unicodedata.category(c) != "Mn"
    )


def _mots_contenu(texte: str) -> set:
    """Mots de contenu normalisés : lettres uniquement, >= 4 caractères,
    racine tronquée (le « s » final est retiré par l'appelant)."""
    mots = set()
    for brut in re.findall(r"[a-zàâäçéèêëîïôöùûüÿæœ]+", _normaliser_mot(texte)):
        if len(brut) >= 4 and brut not in _MOTS_VIDES:
            mots.add(brut)
    return mots


def _racine(mot: str) -> str:
    """Racine de matching : singulier approximatif, jamais < 4 lettres."""
    return mot[:-1] if mot.endswith("s") and len(mot) > 4 else mot


def _sujet_enjeux(query: str) -> Optional[str]:
    """Sujet d'une interrogation sur les enjeux, ou None.

    Le sujet est le texte après le mot « enjeux(x) » : « quels sont les
    enjeux paysagers ? » -> « paysagers ». Sans « enjeux », la demande
    n'est pas du ressort de la règle d'inventaire.
    """
    m = _MOTIFS_ENJEUX.search(query)
    if not m:
        return None
    sujet = query[m.end():].strip().rstrip("?").strip(" .")
    return sujet or None


def _regle_inventaire(
    query: str, search_results: List[Dict], titres: List[str]
) -> Optional[Dict]:
    """Force PARTIEL quand un tome du sujet d'enjeux est absent des passages.

    Déclencheurs cumulatifs, tous requis :
      - la demande interroge les enjeux d'un sujet (_sujet_enjeux) ;
      - un mot de contenu du sujet apparaît dans au moins un titre de
        l'inventaire (racines comparées : « paysagers » matche « paysager ») ;
      - aucun passage de la recherche A ne provient d'un tel document.

    La requête ciblée est assemblée par code : sujet normalisé suivi des
    mots distinctifs du titre (hors termes structurants communs à tous
    les tomes) — jamais par le modèle.
    """
    if not titres:
        return None
    sujet = _sujet_enjeux(query)
    if not sujet:
        return None
    racines_sujet = {_racine(m) for m in _mots_contenu(sujet)}
    if not racines_sujet:
        return None

    tomes_du_sujet = []
    for titre in titres:
        racines_titre = {_racine(m) for m in _mots_contenu(titre)}
        if racines_sujet & racines_titre:
            tomes_du_sujet.append(titre)
    if not tomes_du_sujet:
        return None

    presents = {chunk.get("file_name") for chunk in search_results}
    absents = [t for t in tomes_du_sujet if t not in presents]
    if not absents:
        return None

    # Requête assemblée par code : mots du sujet puis mots distinctifs
    # du titre, dédupliqués par racine (« paysagers » absorbe « paysager »).
    requetes = []
    for titre in absents[:COVERAGE_MAX_QUERIES]:
        termes: List[str] = []
        racines_vues = set()
        for mot in sorted(_mots_contenu(sujet)) + sorted(
            m for m in _mots_contenu(titre) if m not in _TERMES_STRUCTURANTS
        ):
            racine = _racine(mot)
            if racine not in racines_vues:
                racines_vues.add(racine)
                termes.append(mot)
        requete = " ".join(termes).strip()
        if requete:
            requetes.append(requete)
    if not requetes:
        return None

    logger.info(
        f"query2: regle inventaire -> PARTIEL force, "
        f"tomes absents={absents}, requetes={requetes}"
    )
    return {"verdict": "PARTIEL", "manques": absents, "requetes": requetes}


def _sujet_incoherent(sujet: str) -> bool:
    """Détecte une extraction de sujet corrompue ou hors langue.

    Le sujet attendu est un court groupe nominal en français. Le modèle
    multilingual produit parfois du charabia (symboles Latin-1, lettres
    d'autres alphabets) sur les demandes longues — mesuré le 9 août 2026
    sur Beaumont Sud, où le sujet extrait finissait en « ¥å¿ç¼º¤±çæå ».
    Un sujet corrompu garantit une recherche B et une réponse hors sujet.

    Signaux de corruption :
      - lettres hors du répertoire français en proportion notable
        (seuil 15 % : un vrai sujet est à ~100 % de lettres françaises,
        le charabia chute fortement) ;
      - symboles non alphabétiques autres que apostrophe, tiret, espace
        (¥, ¿, ¼, ¤, ±...) ;
      - plus de 12 mots (la consigne en fixe 6).
    """
    if len(sujet.split()) > 12:
        return True
    lettres = sum(1 for c in sujet if c.isalpha())
    if lettres == 0:
        return True
    francaises = len(_LETTRES_FR.findall(sujet))
    if francaises / lettres < 0.85:
        return True
    for c in sujet:
        if c.isalpha():
            # Liste blanche (v2.8) : toute lettre hors du répertoire
            # français signale une sortie corrompue, même en petite
            # proportion — une courte queue CJK (2-4 caractères) passe
            # sous le seuil du ratio 85 % (mesuré sur Beaumont Sud B1,
            # retry du 10 août : « ...éviter réduire补偿Head »).
            if not _LETTRES_FR.match(c):
                return True
        elif c not in " '-":
            return True
    return False


COVERAGE_SYSTEM_PROMPT = (
    "Tu évalues si des extraits documentaires contiennent les informations "
    "nécessaires pour répondre à une demande. Tu réponds uniquement en JSON "
    "valide, sans commentaire ni balise markdown."
)

COVERAGE_USER_TEMPLATE = """Demande :
{query}

Extraits documentaires trouvés (par ordre de pertinence) :
{passages}

Important : la demande peut prendre la forme d'une recommandation, d'une
instruction ou d'une question portant sur un sujet. Les extraits sont
issus de documents de fond : ils ne contiennent JAMAIS la demande
elle-même. N'évalue donc pas si la demande y est mentionnée, mais si
les extraits contiennent les informations de fond permettant d'y
répondre.

Ces extraits contiennent-ils ces informations ?

Réponds en JSON avec exactement ces trois clés :
- "verdict" : "COUVERT" si les extraits contiennent l'essentiel des
  informations, "PARTIEL" s'ils en couvrent une partie, "NON_COUVERT"
  s'ils sont hors sujet ou absents
- "manques" : liste courte des INFORMATIONS manquantes sur le sujet
  (jamais la demande elle-même ; liste vide si COUVERT)
- "requetes" : {max_q} requêtes de recherche maximum, ciblées sur les
  informations manquantes. Règles STRICTES de formulation :
  * 3 à 6 mots de contenu par requête, jamais de phrase
  * une requête = un seul sujet (ne mélange pas plusieurs thèmes)
  * n'emploie que des noms communs et termes techniques, pas de
    verbes d'action ni de mots vides ("impact", "projet", "analyse"
    seuls n'aident pas)
  * les documents cibles emploient souvent un vocabulaire différent
    de la demande : privilégie les synonymes et termes techniques
    français alternatifs (par exemple "variantes" -> "scénarios
    implantation variantes" ; "terres excavées" -> "terres décapées
    stockage"). Ne reprends pas la formulation de la demande
  (liste vide si COUVERT)"""

# Bloc inventaire inséré entre les extraits et la consigne quand le
# catalogue est disponible. La règle d'inventaire neutralise le biais du
# tome de synthèse : une réponse d'apparence complète à partir du RNT ne
# doit plus masquer l'absence du tome spécialisé.
_BLOC_INVENTAIRE = """Inventaire des documents du corpus (titres) :
{titres}

Règle d'inventaire : si le titre d'un document de cet inventaire
correspond clairement au sujet de la demande et qu'AUCUN des extraits
ci-dessus ne provient de ce document, le verdict est PARTIEL — même si
les extraits semblent suffisants — et au moins une requête doit cibler
ce document en reprenant les termes exacts de son titre. Ignore les
documents de l'inventaire sans rapport clair avec la demande."""

# Variante avec catalogue, construite par insertion pour rester
# synchronisée avec COVERAGE_USER_TEMPLATE : le corps de la consigne et
# la spec JSON restent uniques.
COVERAGE_USER_TEMPLATE_CATALOGUE = COVERAGE_USER_TEMPLATE.replace(
    "\nImportant :",
    "\n" + _BLOC_INVENTAIRE + "\n\nImportant :",
)


def _formater_titres(titres: List[str]) -> str:
    """Inventaire à puces pour le prompt, plafonné avec mention du reliquat."""
    if len(titres) > CATALOGUE_PROMPT_MAX_TITRES:
        gardes = titres[:CATALOGUE_PROMPT_MAX_TITRES]
        return "\n".join(f"- {t}" for t in gardes) + (
            f"\n- ... et {len(titres) - len(gardes)} autres documents"
        )
    return "\n".join(f"- {t}" for t in titres)


def _extraire_json(texte: str) -> Optional[Dict]:
    """Extrait le premier objet JSON d'une réponse LLM, tolérant aux
    balises markdown et au texte parasite. Retourne None si échec."""
    if not texte:
        return None
    # Retirer d'éventuelles balises ```json ... ```
    texte = re.sub(r"```(?:json)?", "", texte)
    debut = texte.find("{")
    fin = texte.rfind("}")
    if debut == -1 or fin == -1 or fin <= debut:
        return None
    try:
        return json.loads(texte[debut:fin + 1])
    except (json.JSONDecodeError, ValueError):
        return None


def _texte_passage(chunk: Dict) -> str:
    """Texte d'un passage, quel que soit le champ utilisé par le moteur."""
    return (chunk.get("text") or chunk.get("content") or "").strip()


def _etiquette_passage(chunk: Dict) -> str:
    """Etiquette source d'un passage pour le prompt de couverture."""
    nom = chunk.get("file_name") or "document"
    meta = chunk.get("metadata") or {}
    page = meta.get("page") or meta.get("page_start") or ""
    return f"{nom} p.{page}" if page else nom


class IterativePipeline:
    """Pipeline itératif v2. Encapsule l'analyzer de la route classique :
    toute la recherche et la génération passent par analyzer.analyze(),
    exactement comme /api/query. Seule l'analyse de couverture est du
    code nouveau."""

    def __init__(self, analyzer, catalogue: Optional[CatalogueDocuments] = None):
        self.analyzer = analyzer
        self._catalogue = catalogue

    def _catalogue_titres(self) -> List[str]:
        """Titres du corpus pour le prompt de couverture (v2.9).

        Construction paresseuse depuis le BM25 de l'analyzer ; toute
        indisponibilité se traduit par une liste vide, ce qui laisse le
        prompt de couverture identique à la version sans catalogue.
        """
        if not CATALOGUE_COUVERTURE_ACTIF:
            return []
        if self._catalogue is None:
            bm25 = getattr(self.analyzer.hybrid_search, "bm25_search", None)
            self._catalogue = CatalogueDocuments(bm25)
        return self._catalogue.titres()

    # ------------------------------------------------------------------
    # Étape 2 — analyse de couverture
    # ------------------------------------------------------------------
    def _analyse_couverture(self, query: str, search_results: List[Dict]) -> Dict:
        """1 appel LLM sur les passages COMPLETS. Verdict structuré.

        En cas d'échec de parsing ou d'appel : repli sur COUVERT, c'est-à-dire
        comportement identique à la route classique (jamais pire)."""
        titres = self._catalogue_titres()
        defaut = {"verdict": "COUVERT", "manques": [], "requetes": [],
                  "catalogue_titres": len(titres)}

        passages = []
        for chunk in search_results[:COVERAGE_MAX_PASSAGES]:
            texte = _texte_passage(chunk)[:COVERAGE_PASSAGE_CHARS]
            if texte:
                passages.append(f"[{_etiquette_passage(chunk)}]\n{texte}")

        bloc_passages = "\n\n---\n\n".join(passages) if passages else "Aucun extrait trouvé."

        if titres:
            prompt = COVERAGE_USER_TEMPLATE_CATALOGUE.format(
                query=query,
                passages=bloc_passages,
                max_q=COVERAGE_MAX_QUERIES,
                titres=_formater_titres(titres),
            )
        else:
            # Repli strict : prompt identique à la version sans catalogue.
            prompt = COVERAGE_USER_TEMPLATE.format(
                query=query,
                passages=bloc_passages,
                max_q=COVERAGE_MAX_QUERIES,
            )

        try:
            brut = self.analyzer.llm_generator.call_llm(
                COVERAGE_SYSTEM_PROMPT, prompt
            )
        except Exception as e:
            logger.warning(f"query2: echec appel couverture ({e}), repli COUVERT")
            return defaut

        data = _extraire_json(brut)
        if not data:
            logger.warning("query2: JSON couverture illisible, repli COUVERT")
            return defaut

        verdict = str(data.get("verdict", "COUVERT")).upper().strip()
        if verdict not in ("COUVERT", "PARTIEL", "NON_COUVERT"):
            verdict = "COUVERT"

        manques = data.get("manques") or []
        if not isinstance(manques, list):
            manques = [str(manques)]

        requetes = data.get("requetes") or []
        if not isinstance(requetes, list):
            requetes = [str(requetes)]
        # Garder des requêtes non vides, plafonnées
        requetes = [r.strip() for r in requetes if isinstance(r, str) and r.strip()]
        requetes = requetes[:COVERAGE_MAX_QUERIES]

        # Règle d'inventaire déterministe (v2.10) : prioritaire sur le
        # verdict LLM, dont l'adhérence à la consigne d'inventaire n'est
        # pas garantie. Ne voit que les tomes présents dans l'index.
        forcee = _regle_inventaire(query, search_results, titres)
        if forcee is not None and verdict == "COUVERT":
            return {**forcee, "catalogue_titres": len(titres)}

        if verdict == "COUVERT":
            manques, requetes = [], []

        return {"verdict": verdict, "manques": manques, "requetes": requetes,
                "catalogue_titres": len(titres)}

    # ------------------------------------------------------------------
    # Point d'entrée
    # ------------------------------------------------------------------
    def run(
        self,
        query: str,
        top_k: int = 20,
        custom_prompt: Optional[str] = None,
        history: Optional[List[Dict]] = None,
        max_rounds: int = 1,
    ) -> Dict:
        """Exécute le pipeline. Retourne le résultat analyze() de l'étape
        finale, augmenté d'une clé 'iterative' tracant la couverture."""

        options = {"max_items": top_k}
        if custom_prompt:
            options["custom_prompt"] = custom_prompt

        trace = {
            "version": "pipeline_iteratif_v2",
            "recherche_a": None,
            "couverture": None,
            "recherche_b": {"effectuee": False, "requetes": []},
        }

        # ---------------- Étape 1 : recherche A + génération classique ----
        result_a = self.analyzer.analyze(
            query=query,
            mode="chat",
            options=options,
            history=history,
        )
        search_a = result_a.get("search_results", [])
        trace["recherche_a"] = {"passages": len(search_a)}
        logger.info(f"query2: recherche A -> {len(search_a)} passages")

        # ---------------- Étape 2 : analyse de couverture -----------------
        couverture = self._analyse_couverture(query, search_a)
        trace["couverture"] = couverture
        logger.info(
            f"query2: couverture={couverture['verdict']}, "
            f"requetes={couverture['requetes']}"
        )

        if couverture["verdict"] == "COUVERT" or not couverture["requetes"] or max_rounds < 1:
            # Propriété de sécurité : la réponse retournée EST la réponse
            # de la route classique (mêmes passages, même génération).
            result_a["iterative"] = trace
            return result_a

        # ---------------- Étape 3 : recherche B à quota réservé ---------
        # Reformulation générique de la demande en question directe,
        # utilisée pour la voie générale et la génération. L'étape 1
        # reste sur la demande d'origine : le repli COUVERT retourne
        # toujours la réponse classique à l'identique.
        question = self._reformuler_en_question(query, couverture["requetes"])
        trace["reformulation"] = {"originale": query, "question": question}
        if question != query:
            logger.info(f"query2: demande transformee en question directe")

        result_b = self._recherche_b_quota(
            query, question, couverture["requetes"], options, history, trace
        )
        result_b["iterative"] = trace
        return result_b

    # ------------------------------------------------------------------
    # Étape 3a — extraction du sujet, puis construction de la question
    # ------------------------------------------------------------------
    def _reformuler_en_question(
        self, query: str, requetes: Optional[List[str]] = None
    ) -> str:
        """Construit une question directe à partir du sujet de la demande.

        Deux temps (option C) :
          1. le LLM extrait le SUJET de la demande en 3-6 mots — tâche
             simple et bornée, qui ne laisse pas l'auteur de la demande
             (autorité, client...) devenir le sujet de la question ;
          2. le CODE assemble la question via QUESTION_TEMPLATE — la
             forme interrogative ne dépend plus du modèle.

        Une demande déjà interrogative (contient « ? ») est conservée
        telle quelle, sans appel LLM.

        Cascade de replis (v2.7, v2.8) :
          1. extraction simple ;
          2. retry avec consigne de langue renforcée (v2.7) ;
          3. repli sur la première requête de couverture propre (v2.8) :
             les requêtes ciblées sont le même objet qu'un sujet (3-6
             mots de contenu) mais produites en JSON — un format qui ne
             déraille pas, contrairement au texte libre (mesuré sur
             Beaumont Sud B1 : déraillement DÉTERMINISTE à température 0
             sur « éviter réduire compenser », le retry n'y change rien) ;
          4. repli final sur la demande d'origine : jamais pire.
        """
        if "?" in query:
            return query

        sujet = self._extraire_sujet(query)
        if sujet is not None and _sujet_incoherent(sujet):
            logger.warning(
                f"query2: sujet incoherent ({sujet[:60]!r}), retry renforce"
            )
            sujet = None
        if sujet is None:
            sujet = self._extraire_sujet(query, renforce=True)
            if sujet is not None and _sujet_incoherent(sujet):
                logger.warning(
                    f"query2: sujet toujours incoherent au retry ({sujet[:60]!r})"
                )
                sujet = None
        if sujet is None and requetes:
            for req in requetes:
                req = req.strip().rstrip(".")
                if req and len(req) <= 120 and not _sujet_incoherent(req):
                    sujet = req
                    logger.info(
                        f"query2: repli sur requete couverture: '{req[:80]}'"
                    )
                    break
        if sujet is None:
            logger.warning("query2: extraction impossible, demande conservee")
            return query

        question = QUESTION_TEMPLATE.format(sujet=sujet)
        logger.info(f"query2: sujet='{sujet[:80]}' -> question='{question[:120]}'")
        return question

    def _extraire_sujet(self, query: str, renforce: bool = False) -> Optional[str]:
        """Un appel LLM d'extraction de sujet + nettoyage.

        Retourne le sujet nettoyé, ou None si l'appel a échoué ou si la
        sortie est structurellement invalide (vide, trop courte, trop
        longue). La cohérence linguistique (_sujet_incoherent) est
        jugée par l'appelant, qui décide du retry.
        """
        system = SUJET_SYSTEM_PROMPT_RENFORCE if renforce else SUJET_SYSTEM_PROMPT
        try:
            brut = self.analyzer.llm_generator.call_llm(
                system,
                SUJET_USER_TEMPLATE.format(query=query),
            )
        except Exception as e:
            logger.warning(f"query2: echec extraction sujet ({e})")
            return None

        sujet = (brut or "").strip().strip('"').strip()
        # Une seule ligne, sans préfixe parasite éventuel ni point final
        sujet = sujet.split("\n")[0].strip().rstrip(".").strip()
        sujet = re.sub(r"^sujet\s*[:\-]\s*", "", sujet, flags=re.IGNORECASE)
        if not sujet or len(sujet) < 3 or len(sujet) > 120:
            logger.warning(
                f"query2: sujet extrait invalide ({len(sujet)} car)"
            )
            return None
        return sujet

    # ------------------------------------------------------------------
    # Étape 3 — recherche B à quota réservé
    # ------------------------------------------------------------------
    def _recherche_b_quota(
        self,
        query: str,
        question: str,
        requetes_ciblees: List[str],
        options: Dict,
        history: Optional[List[Dict]],
        trace: Dict,
    ) -> Dict:
        """Recherche B avec places garanties pour les requêtes ciblées.

        Chaque requête ciblée effectue sa propre recherche hybride suivie
        d'un rerank CONTRE ELLE-MÊME (et non contre la demande d'origine),
        puis réserve QUOTA_PAR_REQUETE passages dans le top final. Le
        reste du top est rempli par la recherche générale sur la QUESTION
        REFORMULÉE, rerankée normalement — identique à la route classique.

        La génération réutilise les méthodes exactes de l'analyzer
        (_build_context + llm_generator.generate) : seule la sélection
        des passages change, jamais le prompt ni les paramètres LLM.
        La génération reçoit la question reformulée comme « Question : ».

        Args:
            query: demande d'origine (conservée pour la trace)
            question: forme question de la demande (recherche générale
                et génération)
        """
        analyzer = self.analyzer
        hs = analyzer.hybrid_search
        rr = analyzer.reranker
        fusion_k = analyzer.fusion_top_k          # pool avant rerank (60)
        top_n = analyzer.rerank_top_n             # passages au LLM (30)
        custom_prompt = options.get("custom_prompt")

        def _cid(chunk: Dict):
            return (
                chunk.get("chunk_id")
                or chunk.get("id")
                or (chunk.get("file_name"), _texte_passage(chunk)[:100])
            )

        def _cherche_et_rerank(q: str) -> List[Dict]:
            """Recherche hybride + rerank contre q, comme la route
            classique mais avec q comme unique requête."""
            resultats = hs.search(q, top_k=analyzer.LIMITS["standard"]["max_total_chunks"])
            if rr and resultats:
                resultats = rr.rerank(q, resultats[:fusion_k])
            return resultats

        # ---- Voie générale : la question reformulée --------------------
        # (mesuré : la demande administrative brute est une mauvaise
        # requête de recherche — la forme question cherche mieux)
        generaux = _cherche_et_rerank(question)

        # ---- Voies protégées : une recherche par requête ciblée --------
        proteges: List[Dict] = []
        vus = set()
        detail_quota = []
        for req in requetes_ciblees:
            try:
                classement = _cherche_et_rerank(req)
            except Exception as e:
                logger.warning(f"query2: echec recherche ciblee '{req[:50]}' ({e})")
                continue
            pris = 0
            for chunk in classement:
                cid = _cid(chunk)
                if cid in vus:
                    continue
                vus.add(cid)
                chunk = dict(chunk)
                chunk["quota_requete"] = req
                proteges.append(chunk)
                detail_quota.append({
                    "requete": req,
                    "source": _etiquette_passage(chunk),
                    "rang": pris + 1,
                })
                pris += 1
                if pris >= QUOTA_PAR_REQUETE:
                    break

        # ---- Assemblage : protégés d'abord, puis généraux --------------
        final: List[Dict] = list(proteges)
        for chunk in generaux:
            if len(final) >= top_n:
                break
            cid = _cid(chunk)
            if cid in vus:
                continue
            vus.add(cid)
            final.append(chunk)
        final = final[:top_n]

        trace["recherche_b"] = {
            "effectuee": True,
            "mode": "quota_reserve",
            "requetes": requetes_ciblees,
            "proteges": detail_quota,
            "passages_finaux": len(final),
        }
        logger.info(
            f"query2: recherche B quota -> {len(proteges)} proteges, "
            f"{len(final)} passages au LLM"
        )

        # ---- Génération : appels identiques à la route classique -------
        # Le LLM reçoit la question reformulée après « Question : » —
        # jamais la recommandation administrative brute.
        context = analyzer._build_context(final)
        llm_result = analyzer.llm_generator.generate(
            question,
            context,
            final,
            custom_prompt=custom_prompt,
            history=history,
        )

        return {
            "result_type": "chat",
            "response": llm_result.get("response", ""),
            "sources": llm_result.get("sources", []),
            "search_results": final,
            "metadata": {
                "confidence": llm_result.get("confidence", 0),
                "model": llm_result.get("model", "unknown"),
                "custom_prompt_used": custom_prompt is not None,
                "history_used": history is not None and len(history) > 0,
            },
        }
