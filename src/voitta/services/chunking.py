"""Text chunking service for document processing."""

import re
from dataclasses import dataclass

from ..config import get_settings


@dataclass
class Chunk:
    """A chunk of text with metadata."""

    text: str
    index: int
    start_char: int
    end_char: int


class ChunkingService:
    """Service for splitting text into chunks for embedding."""

    def __init__(
        self,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
        strategy: str | None = None,
    ):
        settings = get_settings()
        self.chunk_size = chunk_size or settings.chunk_size
        self.chunk_overlap = chunk_overlap or settings.chunk_overlap
        self.strategy = strategy or settings.chunking_strategy

    def chunk_text(self, text: str) -> list[Chunk]:
        """Split text into chunks based on configured strategy."""
        if not text or not text.strip():
            return []

        if self.strategy == "recursive":
            return self._recursive_chunk(text)
        elif self.strategy == "sentence":
            return self._sentence_chunk(text)
        elif self.strategy == "fixed":
            return self._fixed_chunk(text)
        else:
            return self._recursive_chunk(text)

    def _recursive_chunk(self, text: str) -> list[Chunk]:
        """Recursively split text using multiple separators.

        Tries to split on paragraphs first, then sentences, then words.
        """
        # Separators in order of preference (most to least meaningful)
        separators = [
            "\n\n",  # Paragraphs
            "\n",  # Lines
            ". ",  # Sentences
            "? ",
            "! ",
            "; ",
            ", ",  # Clauses
            " ",  # Words
            "",  # Characters (last resort)
        ]

        chunks = []
        self._recursive_split(text, separators, 0, chunks)
        return chunks

    def _recursive_split(
        self,
        text: str,
        separators: list[str],
        start_offset: int,
        chunks: list[Chunk],
    ) -> None:
        """Recursively split text and accumulate chunks."""
        if not text:
            return

        # If text fits in one chunk, add it
        if len(text) <= self.chunk_size:
            if text.strip():
                chunks.append(
                    Chunk(
                        text=text.strip(),
                        index=len(chunks),
                        start_char=start_offset,
                        end_char=start_offset + len(text),
                    )
                )
            return

        # Find the best separator to use
        separator = ""
        for sep in separators:
            if sep in text:
                separator = sep
                break

        if not separator:
            # No separator found, split by character
            self._split_by_size(text, start_offset, chunks)
            return

        # Split by separator
        parts = text.split(separator)
        current_chunk = ""
        current_start = start_offset

        for i, part in enumerate(parts):
            # Add separator back (except for last part)
            part_with_sep = part + separator if i < len(parts) - 1 else part

            # Check if adding this part would exceed chunk size
            if len(current_chunk) + len(part_with_sep) <= self.chunk_size:
                current_chunk += part_with_sep
            else:
                # Save current chunk if not empty
                if current_chunk.strip():
                    chunks.append(
                        Chunk(
                            text=current_chunk.strip(),
                            index=len(chunks),
                            start_char=current_start,
                            end_char=current_start + len(current_chunk),
                        )
                    )

                # Handle overlap
                if self.chunk_overlap > 0 and current_chunk:
                    overlap_text = current_chunk[-self.chunk_overlap :]
                    current_chunk = overlap_text + part_with_sep
                    current_start = current_start + len(current_chunk) - len(overlap_text) - len(
                        part_with_sep
                    )
                else:
                    current_chunk = part_with_sep
                    current_start = start_offset + sum(
                        len(parts[j]) + len(separator) for j in range(i)
                    )

                # If single part exceeds chunk size, recursively split it
                if len(part_with_sep) > self.chunk_size:
                    # Find next separator level
                    sep_idx = separators.index(separator)
                    if sep_idx < len(separators) - 1:
                        self._recursive_split(
                            part_with_sep,
                            separators[sep_idx + 1 :],
                            current_start,
                            chunks,
                        )
                        current_chunk = ""

        # Add remaining chunk
        if current_chunk.strip():
            chunks.append(
                Chunk(
                    text=current_chunk.strip(),
                    index=len(chunks),
                    start_char=current_start,
                    end_char=current_start + len(current_chunk),
                )
            )

    def _split_by_size(self, text: str, start_offset: int, chunks: list[Chunk]) -> None:
        """Split text by fixed size with overlap."""
        pos = 0
        while pos < len(text):
            end = min(pos + self.chunk_size, len(text))
            chunk_text = text[pos:end]

            if chunk_text.strip():
                chunks.append(
                    Chunk(
                        text=chunk_text.strip(),
                        index=len(chunks),
                        start_char=start_offset + pos,
                        end_char=start_offset + end,
                    )
                )

            # Move position with overlap
            pos += self.chunk_size - self.chunk_overlap
            if pos >= len(text):
                break

    def _sentence_chunk(self, text: str) -> list[Chunk]:
        """Split text by sentences, combining until chunk size is reached."""
        # Split into sentences
        sentence_pattern = r"(?<=[.!?])\s+"
        sentences = re.split(sentence_pattern, text)

        chunks = []
        current_chunk = ""
        current_start = 0
        pos = 0

        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue

            if len(current_chunk) + len(sentence) + 1 <= self.chunk_size:
                if current_chunk:
                    current_chunk += " " + sentence
                else:
                    current_chunk = sentence
                    current_start = pos
            else:
                if current_chunk:
                    chunks.append(
                        Chunk(
                            text=current_chunk,
                            index=len(chunks),
                            start_char=current_start,
                            end_char=current_start + len(current_chunk),
                        )
                    )
                current_chunk = sentence
                current_start = pos

            pos = text.find(sentence, pos) + len(sentence)

        if current_chunk:
            chunks.append(
                Chunk(
                    text=current_chunk,
                    index=len(chunks),
                    start_char=current_start,
                    end_char=current_start + len(current_chunk),
                )
            )

        return chunks

    def _fixed_chunk(self, text: str) -> list[Chunk]:
        """Split text into fixed-size chunks with overlap."""
        chunks = []
        self._split_by_size(text, 0, chunks)
        return chunks


def get_chunking_service() -> ChunkingService:
    """Get a chunking service instance."""
    return ChunkingService()
