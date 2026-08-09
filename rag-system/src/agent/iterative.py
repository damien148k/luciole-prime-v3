"""
Pipeline itératif v2.7 (endpoint expérimental /api/query2).

Conception : DESIGN_agent_v2_pipeline_iteratif.md (8 août 2026).

Structure fixe en 4 étapes, par opposition à la boucle libre plan/act/observe
de la route agent v1 (mesurée : 11 esquives sur 16 au benchmark MRAe) :

  1. RECHERCHE A + génération : appel identique à la route classique
     (analyzer.analyze mode chat). Si l'étape 2 conclut COUVERT, la réponse
     retournée EST celle de la route classique — la v2 ne peut pas faire
     moins bien que la classique (propriété de sécurité).
  2. ANALYSE DE COUVERTURE : 1 appel LLM qui lit les passages COMPLETS
     (pas des extraits de 400 caractères comme la boucle v1) et répond en
     JSON structuré : verdict, manques, requêtes ciblées.
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
"""

import json
import re
from typing import Dict, List, Optional

from loguru import logger


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
        if not (c.isalpha() or c in " '-"):
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

    def __init__(self, analyzer):
        self.analyzer = analyzer

    # ------------------------------------------------------------------
    # Étape 2 — analyse de couverture
    # ------------------------------------------------------------------
    def _analyse_couverture(self, query: str, search_results: List[Dict]) -> Dict:
        """1 appel LLM sur les passages COMPLETS. Verdict structuré.

        En cas d'échec de parsing ou d'appel : repli sur COUVERT, c'est-à-dire
        comportement identique à la route classique (jamais pire)."""
        defaut = {"verdict": "COUVERT", "manques": [], "requetes": []}

        passages = []
        for chunk in search_results[:COVERAGE_MAX_PASSAGES]:
            texte = _texte_passage(chunk)[:COVERAGE_PASSAGE_CHARS]
            if texte:
                passages.append(f"[{_etiquette_passage(chunk)}]\n{texte}")

        bloc_passages = "\n\n---\n\n".join(passages) if passages else "Aucun extrait trouvé."

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

        if verdict == "COUVERT":
            manques, requetes = [], []

        return {"verdict": verdict, "manques": manques, "requetes": requetes}

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
            f"{len(couverture['requetes'])} requete(s) proposee(s)"
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
        question = self._reformuler_en_question(query)
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
    def _reformuler_en_question(self, query: str) -> str:
        """Construit une question directe à partir du sujet de la demande.

        Deux temps (option C) :
          1. le LLM extrait le SUJET de la demande en 3-6 mots — tâche
             simple et bornée, qui ne laisse pas l'auteur de la demande
             (autorité, client...) devenir le sujet de la question ;
          2. le CODE assemble la question via QUESTION_TEMPLATE — la
             forme interrogative ne dépend plus du modèle.

        Une demande déjà interrogative (contient « ? ») est conservée
        telle quelle, sans appel LLM.

        Extraction en deux chances (v2.7) : le décrochage vers une
        sortie corrompue est un glitch stochastique du modèle
        multilingual — une seconde tentative avec consigne renforcée
        le corrige le plus souvent. Le repli sur la demande d'origine
        reste le filet final : jamais pire.
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
