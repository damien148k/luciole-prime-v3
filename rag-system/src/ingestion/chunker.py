"""
Document Chunker — Intelligent text chunking strategies
V3 : Chunking adaptatif par format de fichier

Enrichit chaque chunk avec le contexte du fichier (chemin + nom) pour améliorer la recherche.
Stratégies spéciales :
  - XLSX : l'en-tête (ligne 1) est répétée dans chaque chunk
  - PPTX : 1 chunk = 1 slide complet (titre + corps + notes)
  - MSG/EML : métadonnées (De, À, Objet, Date) incluses dans chaque chunk
"""

from typing import List, Dict, Optional
from dataclasses import dataclass
from loguru import logger
from pathlib import Path
import re
import yaml


# Les analyseurs de documents annoncent un type qui ne correspond pas toujours
# a la clef utilisee dans chunking_strategies de settings.yaml. Sans cette
# table, les strategies 'xlsx' et 'txt' n'etaient jamais appliquees.
TYPE_ALIASES = {
    "excel": "xlsx",
    "text": "txt",
    "doc": "docx",
    "ppt": "pptx",
    "email": "msg",
}


@dataclass
class Chunk:
    """Represents a document chunk."""
    text: str
    text_with_context: str
    chunk_id: str
    document_id: str
    file_path: str
    file_name: str
    start_char: int
    end_char: int
    metadata: Dict


class Chunker:
    """
    Intelligent document chunker with multiple strategies.
    V3 : supporte le chunking adaptatif par type de fichier via settings.yaml.
    """

    def __init__(
        self,
        chunk_size: int = 512,
        chunk_overlap: int = 50,
        strategy: str = "sentence",
        include_file_context: bool = True,
        adaptive: bool = False,
        chunking_strategies: Dict = None
    ):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.strategy = strategy
        self.include_file_context = include_file_context
        self.adaptive = adaptive
        self.chunking_strategies = chunking_strategies or {}

        self.sentence_pattern = re.compile(r'(?<=[.!?])\s+')

        logger.info(
            f"Chunker initialized: size={chunk_size}, overlap={chunk_overlap}, "
            f"strategy={strategy}, context={include_file_context}, adaptive={adaptive}"
        )
        if adaptive:
            if self.chunking_strategies:
                detail = ", ".join(
                    f"{t}={c.get('chunk_size', chunk_size)}/{c.get('chunk_overlap', chunk_overlap)}"
                    f":{c.get('strategy', strategy)}"
                    for t, c in sorted(self.chunking_strategies.items())
                )
                logger.info(f"Chunker adaptatif : {detail}")
            else:
                logger.warning(
                    "Chunker adaptatif demande mais aucune strategie par type "
                    "n'a ete fournie : les valeurs globales seront appliquees "
                    "a tous les formats."
                )

    @classmethod
    def from_config(cls, config_path: str = "config/settings.yaml") -> "Chunker":
        """Construit un Chunker depuis le fichier de configuration complet."""
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        chunking = config.get("chunking", {})
        return cls(
            chunk_size=chunking.get("chunk_size", 512),
            chunk_overlap=chunking.get("chunk_overlap", 50),
            strategy=chunking.get("strategy", "sentence"),
            include_file_context=chunking.get("include_file_context", True),
            adaptive=chunking.get("adaptive", False),
            chunking_strategies=config.get("chunking_strategies", {}),
        )

    def _resolve_strategy(self, doc_type: str) -> Dict:
        """Résout la stratégie de chunking pour un type de document donné."""
        if not self.adaptive or not self.chunking_strategies:
            return {
                "strategy": self.strategy,
                "chunk_size": self.chunk_size,
                "chunk_overlap": self.chunk_overlap,
            }

        type_lower = TYPE_ALIASES.get(doc_type.lower(), doc_type.lower())
        if type_lower in self.chunking_strategies:
            cfg = self.chunking_strategies[type_lower]
            return {
                "strategy": cfg.get("strategy", self.strategy),
                "chunk_size": cfg.get("chunk_size", self.chunk_size),
                "chunk_overlap": cfg.get("chunk_overlap", self.chunk_overlap),
                "rows_per_chunk": cfg.get("rows_per_chunk"),
                "include_header": cfg.get("include_header", True),
            }

        return {
            "strategy": self.strategy,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
        }

    def chunk(self, document: Dict) -> List[Chunk]:
        content = document.get("content", "")
        metadata = document.get("metadata", {})
        file_path = metadata.get("file_path", "")
        file_name = metadata.get("file_name", "unknown")
        doc_id = file_name
        doc_type = metadata.get("type", "")

        if not content.strip():
            logger.warning(f"Empty content for document: {doc_id}")
            return []

        file_context = self._build_file_context(file_path, file_name, metadata)

        resolved = self._resolve_strategy(doc_type)
        strat = resolved["strategy"]

        if strat == "slide":
            return self._chunk_by_slide(content, doc_id, file_path, file_name, file_context, metadata, resolved)
        elif strat == "email":
            return self._chunk_email(content, doc_id, file_path, file_name, file_context, metadata, resolved)
        elif strat == "paragraph":
            return self._chunk_by_paragraph(content, doc_id, file_path, file_name, file_context, metadata, resolved)
        elif strat == "sentence":
            return self._chunk_by_sentence(content, doc_id, file_path, file_name, file_context, metadata, resolved)
        else:
            return self._chunk_fixed(content, doc_id, file_path, file_name, file_context, metadata, resolved)

    # =========================================================================
    # FILE CONTEXT
    # =========================================================================

    def _build_file_context(self, file_path: str, file_name: str, metadata: Dict) -> str:
        path_obj = Path(file_path) if file_path else None
        parent_path = ""
        if path_obj and path_obj.parent:
            parts = path_obj.parent.parts[-3:]
            parent_path = "/".join(parts) if parts else ""

        doc_type = metadata.get("type", "document").upper()

        context_parts = [f"Fichier: {file_name}"]
        if parent_path:
            context_parts.append(f"Chemin: {parent_path}")
        context_parts.append(f"Type: {doc_type}")

        if metadata.get("title"):
            context_parts.append(f"Titre: {metadata['title']}")
        if metadata.get("author"):
            context_parts.append(f"Auteur: {metadata['author']}")
        if metadata.get("subject"):
            context_parts.append(f"Sujet: {metadata['subject']}")

        return "[" + " | ".join(context_parts) + "]"

    def _make_chunk(self, text: str, file_context: str, doc_id: str, file_path: str, file_name: str, metadata: Dict, chunk_idx: int, start_char: int, end_char: int) -> Chunk:
        text_with_context = f"{file_context}\n{text}" if self.include_file_context else text
        return Chunk(
            text=text,
            text_with_context=text_with_context,
            chunk_id=f"{doc_id}_chunk_{chunk_idx}",
            document_id=doc_id,
            file_path=file_path,
            file_name=file_name,
            start_char=start_char,
            end_char=end_char,
            metadata={**metadata, "chunk_index": chunk_idx}
        )

    # =========================================================================
    # SENTENCE CHUNKING
    # =========================================================================

    def _split_oversized(self, text: str, limit: int, overlap: int) -> List[str]:
        """
        Decoupe une unite (phrase, paragraphe) plus longue que la limite.

        Le decoupage en phrases repose sur la ponctuation. Un tableau, un
        sommaire, une legende de carte ou une liste a puces n'en contiennent
        pas : l'unite produite peut alors faire plusieurs dizaines de milliers
        de caracteres. Sans borne, ce fragment part tel quel vers l'encodeur
        et sature la memoire du GPU.

        La coupure est cherchee sur un caractere blanc en fin de fenetre afin
        de ne pas tronquer un mot au milieu.
        """
        if limit <= 0 or len(text) <= limit:
            return [text]

        pas = max(1, limit - max(0, overlap))
        morceaux: List[str] = []
        debut = 0

        while debut < len(text):
            fin = min(debut + limit, len(text))
            if fin < len(text):
                fenetre = text.rfind(" ", debut + pas // 2, fin)
                if fenetre > debut:
                    fin = fenetre
            morceau = text[debut:fin].strip()
            if morceau:
                morceaux.append(morceau)
            if fin >= len(text):
                break
            suivant = max(fin - max(0, overlap), debut + 1)
            if overlap > 0 and suivant < len(text) and not text[suivant - 1].isspace():
                blanc = text.find(" ", suivant, fin)
                if blanc != -1 and blanc + 1 < fin:
                    suivant = blanc + 1
            debut = suivant

        return morceaux or [text[:limit]]

    @staticmethod
    def _queue_recouvrement(texte: str, co: int) -> str:
        """
        Queue de recouvrement d'un fragment, alignee sur un debut de mot.

        Le recouvrement reprend les `co` derniers caracteres du fragment
        precedent. Pris tels quels, ils tombent presque toujours au milieu
        d'un mot : le fragment suivant commencait par "ces et reseau" pour
        "acces et reseau", ou "vant d'etre delivree" pour "avant d'etre
        delivree". Constate sur les etudes d'impact du corpus wpd, ou six
        extraits sur quinze transmis au modele demarraient ainsi.

        Le cout est double : l'extrait presente au modele s'ouvre sur un
        mot mutile, et le vecteur dense est calcule sur ce meme texte.

        La queue est donc avancee jusqu'a la premiere frontiere de mot.
        Elle en ressort au plus egale a `co`, jamais plus longue, ce qui
        preserve la borne de taille des fragments. Si aucune frontiere de
        mot ne s'y trouve, le recouvrement est abandonne plutot que
        tronque.
        """
        if co <= 0 or not texte:
            return ""
        if len(texte) <= co:
            # Comportement anterieur conserve : un fragment plus court que
            # le recouvrement ne produit pas de queue. Le renvoyer entier
            # rendrait le fragment suivant identique au precedent.
            return ""

        debut = len(texte) - co
        # Deja aligne : le caractere precedent est un blanc.
        if texte[debut - 1].isspace():
            return texte[debut:]

        avance = re.search(r"\s", texte[debut:])
        if avance is None:
            # Aucune frontiere de mot dans la queue : elle tombe a
            # l'interieur d'un jeton unique plus long qu'elle (URL,
            # identifiant, cellule de tableau compacte), ou le recouvrement
            # demande est plus court qu'un mot. Recouvrir avec une bribe de
            # jeton n'apporte aucun contexte au fragment suivant et lui fait
            # ouvrir sur un mot mutile. On renonce au recouvrement.
            return ""

        return texte[debut + avance.end():]

    def _chunk_by_sentence(self, content, doc_id, file_path, file_name, file_context, metadata, resolved):
        cs = resolved.get("chunk_size", self.chunk_size)
        co = resolved.get("chunk_overlap", self.chunk_overlap)

        sentences = self.sentence_pattern.split(content)
        chunks = []
        current_chunk = ""
        start_char = 0
        chunk_idx = 0

        unites = []
        surdimensionnees = 0
        for brute in sentences:
            brute = brute.strip()
            if not brute:
                continue
            if len(brute) > cs:
                surdimensionnees += 1
                # Le fragment suivant demarre avec le recouvrement du
                # precedent : on lui reserve sa place pour que la somme des
                # deux ne depasse jamais la taille demandee.
                unites.extend(self._split_oversized(brute, max(1, cs - co), co))
            else:
                unites.append(brute)

        if surdimensionnees:
            logger.warning(
                f"{doc_id} : {surdimensionnees} unite(s) sans ponctuation depassant "
                f"{cs} caracteres ont ete redecoupees (tableaux, sommaires ou listes)."
            )

        for sentence in unites:
            if len(current_chunk) + len(sentence) > cs and current_chunk:
                text = current_chunk.strip()
                chunks.append(self._make_chunk(text, file_context, doc_id, file_path, file_name, metadata, chunk_idx, start_char, start_char + len(current_chunk)))
                chunk_idx += 1
                overlap_text = self._queue_recouvrement(current_chunk, co)
                start_char = start_char + len(current_chunk) - len(overlap_text)
                current_chunk = overlap_text

            current_chunk += sentence + " "

        if current_chunk.strip():
            text = current_chunk.strip()
            chunks.append(self._make_chunk(text, file_context, doc_id, file_path, file_name, metadata, chunk_idx, start_char, start_char + len(current_chunk)))

        logger.info(f"Created {len(chunks)} chunks for document: {doc_id} (sentence)")
        return chunks

    # =========================================================================
    # PARAGRAPH CHUNKING
    # =========================================================================

    def _chunk_by_paragraph(self, content, doc_id, file_path, file_name, file_context, metadata, resolved):
        cs = resolved.get("chunk_size", self.chunk_size)
        co = resolved.get("chunk_overlap", self.chunk_overlap)

        paragraphs = content.split("\n\n")
        chunks = []
        current_chunk = ""
        start_char = 0
        chunk_idx = 0

        unites = []
        surdimensionnes = 0
        for brut in paragraphs:
            brut = brut.strip()
            if not brut:
                continue
            if len(brut) > cs:
                surdimensionnes += 1
                unites.extend(self._split_oversized(brut, cs, co))
            else:
                unites.append(brut)

        if surdimensionnes:
            logger.warning(
                f"{doc_id} : {surdimensionnes} paragraphe(s) depassant {cs} "
                f"caracteres ont ete redecoupes."
            )

        for para in unites:
            if len(current_chunk) + len(para) > cs and current_chunk:
                text = current_chunk.strip()
                chunks.append(self._make_chunk(text, file_context, doc_id, file_path, file_name, metadata, chunk_idx, start_char, start_char + len(current_chunk)))
                chunk_idx += 1
                start_char = start_char + len(current_chunk)
                current_chunk = ""

            current_chunk += para + "\n\n"

        if current_chunk.strip():
            text = current_chunk.strip()
            chunks.append(self._make_chunk(text, file_context, doc_id, file_path, file_name, metadata, chunk_idx, start_char, start_char + len(current_chunk)))

        logger.info(f"Created {len(chunks)} chunks for document: {doc_id} (paragraph)")
        return chunks

    # =========================================================================
    # SLIDE CHUNKING (PPTX)
    # =========================================================================

    def _chunk_by_slide(self, content, doc_id, file_path, file_name, file_context, metadata, resolved):
        slide_pattern = re.compile(r'---\s*Slide\s+\d+\s*---')
        parts = slide_pattern.split(content)
        headers = slide_pattern.findall(content)

        chunks = []
        chunk_idx = 0
        pos = 0

        for i, part in enumerate(parts):
            part = part.strip()
            if not part:
                pos += len(part) + (len(headers[i]) if i < len(headers) else 0)
                continue

            slide_header = headers[i - 1] if i > 0 and i - 1 < len(headers) else ""
            text = f"{slide_header}\n{part}".strip() if slide_header else part

            chunks.append(self._make_chunk(text, file_context, doc_id, file_path, file_name, metadata, chunk_idx, pos, pos + len(text)))
            chunk_idx += 1
            pos += len(part) + (len(headers[i]) if i < len(headers) else 0)

        if not chunks and content.strip():
            chunks.append(self._make_chunk(content.strip(), file_context, doc_id, file_path, file_name, metadata, 0, 0, len(content)))

        logger.info(f"Created {len(chunks)} chunks for document: {doc_id} (slide)")
        return chunks

    # =========================================================================
    # EMAIL CHUNKING (MSG / EML)
    # =========================================================================

    def _chunk_email(self, content, doc_id, file_path, file_name, file_context, metadata, resolved):
        cs = resolved.get("chunk_size", 800)

        lines = content.split("\n")
        meta_lines = []
        body_start = 0

        for i, line in enumerate(lines):
            if line.strip().lower().startswith(("subject:", "from:", "to:", "date:", "de:", "à:", "objet:")):
                meta_lines.append(line.strip())
                body_start = i + 1
            elif not line.strip() and meta_lines:
                body_start = i + 1
                break

        email_header = "\n".join(meta_lines)
        body = "\n".join(lines[body_start:]).strip()

        if not body:
            text = email_header if email_header else content.strip()
            return [self._make_chunk(text, file_context, doc_id, file_path, file_name, metadata, 0, 0, len(content))]

        chunks = []
        chunk_idx = 0
        sentences = self.sentence_pattern.split(body)
        current = ""

        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue

            if len(current) + len(sentence) > cs and current:
                full_text = f"{email_header}\n\n{current.strip()}" if email_header else current.strip()
                chunks.append(self._make_chunk(full_text, file_context, doc_id, file_path, file_name, metadata, chunk_idx, 0, len(full_text)))
                chunk_idx += 1
                current = ""

            current += sentence + " "

        if current.strip():
            full_text = f"{email_header}\n\n{current.strip()}" if email_header else current.strip()
            chunks.append(self._make_chunk(full_text, file_context, doc_id, file_path, file_name, metadata, chunk_idx, 0, len(full_text)))

        logger.info(f"Created {len(chunks)} chunks for document: {doc_id} (email)")
        return chunks

    # =========================================================================
    # FIXED CHUNKING
    # =========================================================================

    def _chunk_fixed(self, content, doc_id, file_path, file_name, file_context, metadata, resolved):
        cs = resolved.get("chunk_size", self.chunk_size)
        co = resolved.get("chunk_overlap", self.chunk_overlap)

        chunks = []
        start = 0
        chunk_idx = 0

        while start < len(content):
            end = min(start + cs, len(content))
            text = content[start:end].strip()
            chunks.append(self._make_chunk(text, file_context, doc_id, file_path, file_name, metadata, chunk_idx, start, end))
            chunk_idx += 1
            start = end - co
            if start >= len(content) - co:
                break

        logger.info(f"Created {len(chunks)} chunks for document: {doc_id} (fixed)")
        return chunks
