"""llm-tldr companion indexing.

Runs `llm-tldr` static analysis over a synced Git repo and stores the
structural summaries as companion chunks in Qdrant alongside the raw code
chunks. Each chunk is tagged with ``source_type="llm-tldr-analysis"`` and
``related_file`` pointing back to the originating source file.

Phase 2: per-file incremental reindex. Source files are hashed; only files
whose hash changed (or are newly added or removed) trigger an extract /
chunk / store cycle. Set ``force=True`` to wipe and re-extract everything
(useful after an llm-tldr library bump).
"""

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..db.models import LlmTldrIndexedFile
from .chunking import ChunkingService, get_chunking_service
from .embedding import EmbeddingService, get_embedding_service
from .sparse_embedding import SparseEmbeddingService, get_sparse_embedding_service
from .vector_store import ChunkMetadata, VectorStoreService, get_vector_store

logger = logging.getLogger(__name__)

SOURCE_TYPE = "llm-tldr-analysis"

# Subset of llm-tldr's supported extensions. Keep small for PoC; expand later.
SUPPORTED_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java",
    ".rb", ".php", ".swift", ".cs", ".kt", ".kts", ".scala", ".sc",
    ".lua", ".luau", ".ex", ".exs", ".c", ".h", ".cpp", ".hpp",
    ".cc", ".cxx", ".hh",
}

# Skip pathologically large source files — tree-sitter parsing cost grows
# super-linearly and a single 10MB generated file would block sync.
MAX_FILE_BYTES = 1_000_000


class LlmTldrIndexer:
    """Indexes llm-tldr structural analysis as companion chunks."""

    def __init__(
        self,
        chunker: ChunkingService | None = None,
        embedder: EmbeddingService | None = None,
        sparse_embedder: SparseEmbeddingService | None = None,
        vector_store: VectorStoreService | None = None,
    ):
        self.chunker = chunker or get_chunking_service()
        self.embedder = embedder or get_embedding_service()
        self.sparse_embedder = sparse_embedder or get_sparse_embedding_service()
        self.vector_store = vector_store or get_vector_store()

    def index_repo(
        self,
        repo_dir: Path,
        folder_path: str,
        index_folder: str,
        db: Session,
        force: bool = False,
    ) -> dict:
        """Incrementally re-index llm-tldr companion chunks for ``repo_dir``.

        Compares current source-file SHA-256 hashes against the
        ``LlmTldrIndexedFile`` rows previously recorded for ``folder_path``
        and only re-extracts the analysis for files that are new, changed,
        or removed.

        Args:
            repo_dir: Local directory containing the cloned source files.
            folder_path: Voitta-RAG folder_path the chunks belong to (e.g.
                "myrepo/branches/main"). Companion chunks for this folder
                are scoped by this value.
            index_folder: Folder at which indexing was triggered (the
                top-level Git source folder, used for filtered searches).
            db: Active SQLAlchemy session.
            force: If True, wipes every companion chunk for the folder and
                re-extracts every file. Use after an llm-tldr library bump
                or when the rendering format changes.

        Returns:
            Stats dict with files_new, files_updated, files_removed,
            files_unchanged, files_skipped, chunks_stored, errors counts.
        """
        try:
            from tldr.api import extract_file  # type: ignore
        except ImportError as e:
            logger.error(
                "llm-tldr is not installed; skipping companion indexing: %s", e,
            )
            return {
                "files_new": 0, "files_updated": 0, "files_removed": 0,
                "files_unchanged": 0, "files_skipped": 0,
                "chunks_stored": 0, "errors": 1,
            }

        stats = {
            "files_new": 0, "files_updated": 0, "files_removed": 0,
            "files_unchanged": 0, "files_skipped": 0,
            "chunks_stored": 0, "errors": 0,
        }

        if force:
            wiped = self.vector_store.delete_by_folder_and_source_type(
                folder_path, SOURCE_TYPE,
            )
            logger.info(
                "llm-tldr: force=True wiped %d chunks for %s", wiped, folder_path,
            )
            db.execute(
                delete(LlmTldrIndexedFile).where(
                    LlmTldrIndexedFile.folder_path == folder_path
                )
            )
            db.commit()

        # Snapshot known state from the DB.
        existing_rows = db.execute(
            select(LlmTldrIndexedFile).where(
                LlmTldrIndexedFile.folder_path == folder_path
            )
        ).scalars().all()
        existing_by_rel: dict[str, LlmTldrIndexedFile] = {
            row.related_file: row for row in existing_rows
        }

        # Snapshot current source-file hashes.
        files = _collect_source_files(repo_dir)
        current_hashes: dict[str, str] = {}
        for src in files:
            rel = str(src.relative_to(repo_dir))
            try:
                current_hashes[rel] = _hash_file(src)
            except OSError as e:
                logger.debug("llm-tldr: unreadable %s: %s", rel, e)
                stats["files_skipped"] += 1

        logger.info(
            "llm-tldr: scanning %s — %d source files on disk, %d previously indexed",
            folder_path, len(current_hashes), len(existing_by_rel),
        )

        # Removed files: in DB, not on disk → delete their chunks + rows.
        # Commit per file to keep each SQLite write transaction tiny. With
        # journal_mode=DELETE the SQLite file lock is held for the whole
        # transaction, blocking any concurrent writer (e.g. the FastAPI
        # request handler that triggered the sync). A single transaction
        # spanning the entire indexer pass routinely exceeds busy_timeout
        # and surfaces as "database is locked" after partial progress.
        removed = set(existing_by_rel) - set(current_hashes)
        for rel in removed:
            self.vector_store.delete_by_folder_and_related_file(folder_path, rel)
            db.delete(existing_by_rel[rel])
            stats["files_removed"] += 1
            db.commit()

        # New / changed files: extract, store, upsert row.
        for rel, source_hash in current_hashes.items():
            existing_row = existing_by_rel.get(rel)
            if existing_row is not None and existing_row.content_hash == source_hash:
                stats["files_unchanged"] += 1
                continue

            src = repo_dir / rel
            try:
                info = extract_file(str(src), base_path=str(repo_dir))
            except Exception as e:
                logger.debug("llm-tldr extract failed for %s: %s", rel, e)
                stats["files_skipped"] += 1
                continue

            text = _render_extract(rel, info)
            if not text.strip():
                stats["files_skipped"] += 1
                continue

            # Replace any prior chunks for this file before reinserting.
            if existing_row is not None:
                self.vector_store.delete_by_folder_and_related_file(
                    folder_path, rel,
                )

            try:
                chunks_stored = self._store_chunks(
                    text=text,
                    related_file=rel,
                    folder_path=folder_path,
                    index_folder=index_folder,
                )
            except Exception as e:
                logger.exception(
                    "llm-tldr: failed to store chunks for %s: %s", rel, e,
                )
                stats["errors"] += 1
                continue

            if existing_row is None:
                db.add(
                    LlmTldrIndexedFile(
                        folder_path=folder_path,
                        related_file=rel,
                        content_hash=source_hash,
                        chunk_count=chunks_stored,
                    )
                )
                stats["files_new"] += 1
            else:
                existing_row.content_hash = source_hash
                existing_row.chunk_count = chunks_stored
                existing_row.updated_at = datetime.now(timezone.utc)
                stats["files_updated"] += 1
            stats["chunks_stored"] += chunks_stored
            db.commit()

        logger.info("llm-tldr: indexing complete for %s: %s", folder_path, stats)
        return stats

    def _store_chunks(
        self,
        text: str,
        related_file: str,
        folder_path: str,
        index_folder: str,
    ) -> int:
        chunks = self.chunker.chunk_text(text)
        if not chunks:
            return 0

        texts = [c.text for c in chunks]
        embeddings = self.embedder.embed_texts(texts)
        sparse_vectors = self.sparse_embedder.embed_texts(texts)

        indexed_at = datetime.now(timezone.utc).isoformat()
        synthetic_path = f"llm-tldr://{folder_path}/{related_file}"
        chunk_data = []
        for chunk, embedding in zip(chunks, embeddings):
            metadata = ChunkMetadata(
                file_path=synthetic_path,
                folder_path=folder_path,
                index_folder=index_folder,
                file_name=Path(related_file).name + ".tldr.md",
                chunk_index=chunk.index,
                total_chunks=len(chunks),
                start_char=chunk.start_char,
                end_char=chunk.end_char,
                indexed_at=indexed_at,
                source_type=SOURCE_TYPE,
                related_file=related_file,
            )
            chunk_data.append((chunk.text, embedding, metadata))

        self.vector_store.store_chunks(chunk_data, sparse_vectors=sparse_vectors)
        return len(chunks)


def _collect_source_files(repo_dir: Path) -> list[Path]:
    files: list[Path] = []
    for entry in repo_dir.rglob("*"):
        if not entry.is_file():
            continue
        if any(part.startswith(".") for part in entry.relative_to(repo_dir).parts):
            continue
        if entry.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        try:
            if entry.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        files.append(entry)
    return files


def _render_extract(rel_path: str, info: dict) -> str:
    """Render an llm-tldr extract_file dict as markdown text for chunking."""
    lines: list[str] = []
    lines.append(f"# llm-tldr analysis: {rel_path}")
    lang = info.get("language") or "unknown"
    lines.append(f"Language: {lang}")
    docstring = info.get("docstring")
    if docstring:
        lines.append("")
        lines.append("## Module docstring")
        lines.append(docstring.strip())

    imports = info.get("imports") or []
    if imports:
        lines.append("")
        lines.append("## Imports")
        for imp in imports:
            lines.append(f"- {_render_import(imp)}")

    functions = info.get("functions") or []
    if functions:
        lines.append("")
        lines.append("## Functions")
        for fn in functions:
            lines.append(_render_function(fn))

    classes = info.get("classes") or []
    if classes:
        lines.append("")
        lines.append("## Classes")
        for cls in classes:
            lines.append(_render_class(cls))

    call_graph = info.get("call_graph") or {}
    if call_graph:
        lines.append("")
        lines.append("## Call graph")
        lines.append("```json")
        lines.append(json.dumps(call_graph, indent=2, default=str))
        lines.append("```")

    return "\n".join(lines)


def _render_import(imp) -> str:
    if isinstance(imp, dict):
        module = imp.get("module") or imp.get("name") or ""
        names = imp.get("names") or []
        if names:
            return f"{module} ({', '.join(map(str, names))})"
        return str(module)
    return str(imp)


def _render_function(fn) -> str:
    if not isinstance(fn, dict):
        return f"- {fn}"
    parts: list[str] = []
    sig = fn.get("signature") or fn.get("name") or ""
    parts.append(f"### {sig}")
    if fn.get("docstring"):
        parts.append(fn["docstring"].strip())
    calls = fn.get("calls") or []
    if calls:
        parts.append(f"Calls: {', '.join(map(str, calls))}")
    called_by = fn.get("called_by") or []
    if called_by:
        parts.append(f"Called by: {', '.join(map(str, called_by))}")
    complexity = fn.get("complexity")
    if complexity is not None:
        parts.append(f"Cyclomatic complexity: {complexity}")
    return "\n".join(parts)


def _render_class(cls) -> str:
    if not isinstance(cls, dict):
        return f"- {cls}"
    parts: list[str] = []
    name = cls.get("name") or "(anonymous)"
    bases = cls.get("bases") or cls.get("base_classes") or []
    header = f"### class {name}"
    if bases:
        header += f"({', '.join(map(str, bases))})"
    parts.append(header)
    if cls.get("docstring"):
        parts.append(cls["docstring"].strip())
    methods = cls.get("methods") or []
    for m in methods:
        parts.append(_render_function(m))
    return "\n".join(parts)


def _hash_file(path: Path) -> str:
    """SHA-256 hash of the file's raw bytes."""
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    return sha.hexdigest()


def get_llm_tldr_indexer() -> LlmTldrIndexer:
    return LlmTldrIndexer()
