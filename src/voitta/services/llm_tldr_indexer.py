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

            parts = _build_chunk_parts(rel, info)
            if not parts:
                stats["files_skipped"] += 1
                continue

            # Replace any prior chunks for this file before reinserting.
            if existing_row is not None:
                self.vector_store.delete_by_folder_and_related_file(
                    folder_path, rel,
                )

            try:
                chunks_stored = self._store_chunk_parts(
                    parts=parts,
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

    def _store_chunk_parts(
        self,
        parts: list[tuple[str, dict]],
        related_file: str,
        folder_path: str,
        index_folder: str,
    ) -> int:
        """Embed and store pre-built (text, payload-overrides) tuples.

        Each part becomes one Qdrant point. The text body goes through
        the dense+sparse embedder; ``payload_overrides`` contains the
        Phase 3 call-graph fields specific to that chunk (function name,
        callers/callees, etc.).
        """
        if not parts:
            return 0

        texts = [text for text, _ in parts]
        embeddings = self.embedder.embed_texts(texts)
        sparse_vectors = self.sparse_embedder.embed_texts(texts)

        indexed_at = datetime.now(timezone.utc).isoformat()
        synthetic_path = f"llm-tldr://{folder_path}/{related_file}"
        chunk_data = []
        for idx, ((text, payload_overrides), embedding) in enumerate(
            zip(parts, embeddings)
        ):
            metadata = ChunkMetadata(
                file_path=synthetic_path,
                folder_path=folder_path,
                index_folder=index_folder,
                file_name=Path(related_file).name + ".tldr.md",
                chunk_index=idx,
                total_chunks=len(parts),
                start_char=0,
                end_char=len(text),
                indexed_at=indexed_at,
                source_type=SOURCE_TYPE,
                related_file=related_file,
                tldr_chunk_kind=payload_overrides.get("tldr_chunk_kind"),
                tldr_function_name=payload_overrides.get("tldr_function_name"),
                tldr_class_name=payload_overrides.get("tldr_class_name"),
                tldr_callees=payload_overrides.get("tldr_callees"),
                tldr_callers=payload_overrides.get("tldr_callers"),
                tldr_caller_count=payload_overrides.get("tldr_caller_count"),
                tldr_callee_count=payload_overrides.get("tldr_callee_count"),
                tldr_imports=payload_overrides.get("tldr_imports"),
            )
            chunk_data.append((text, embedding, metadata))

        self.vector_store.store_chunks(chunk_data, sparse_vectors=sparse_vectors)
        return len(chunk_data)


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


# Bumped whenever the analysis chunk format changes. Mixed into
# _hash_file so existing rows whose content_hash was computed under a
# previous format will mismatch and trigger a re-extract on the next
# sync. v1: file-level rendered markdown (Phase 1+2). v3: per-function
# chunks + call-graph payload (Phase 3).
ANALYSIS_FORMAT_VERSION = "v3"


def _build_chunk_parts(rel_path: str, info: dict) -> list[tuple[str, dict]]:
    """Build (text, payload-overrides) tuples for one source file.

    Emits one file_overview chunk plus one function chunk per top-level
    function and per class method. Each function chunk carries the
    structured call-graph payload (callers, callees, counts, imports)
    needed for Phase 3 filtered searches.
    """
    parts: list[tuple[str, dict]] = []

    # File overview
    imports_list = _normalize_imports(info.get("imports") or [])
    lang = info.get("language") or "unknown"
    overview_lines = [f"# llm-tldr file overview: {rel_path}"]
    overview_lines.append(f"Language: {lang}")
    docstring = info.get("docstring")
    if docstring:
        overview_lines.append("")
        overview_lines.append(docstring.strip())
    if imports_list:
        overview_lines.append("")
        overview_lines.append("Imports: " + ", ".join(imports_list))
    class_names = [
        c.get("name") for c in (info.get("classes") or [])
        if isinstance(c, dict) and c.get("name")
    ]
    if class_names:
        overview_lines.append("Classes: " + ", ".join(class_names))
    func_names = [
        f.get("name") for f in (info.get("functions") or [])
        if isinstance(f, dict) and f.get("name")
    ]
    if func_names:
        overview_lines.append("Top-level functions: " + ", ".join(func_names))
    parts.append((
        "\n".join(overview_lines),
        {
            "tldr_chunk_kind": "file_overview",
            "tldr_imports": imports_list or None,
        },
    ))

    call_graph = info.get("call_graph") or {}
    calls_map = call_graph.get("calls") or {}
    called_by_map = call_graph.get("called_by") or {}

    # Top-level functions
    for fn in info.get("functions") or []:
        part = _build_function_part(
            fn, class_name=None, rel_path=rel_path,
            imports_list=imports_list,
            calls_map=calls_map, called_by_map=called_by_map,
        )
        if part is not None:
            parts.append(part)

    # Class methods
    for cls in info.get("classes") or []:
        if not isinstance(cls, dict):
            continue
        cls_name = cls.get("name") or "(anonymous)"
        for method in cls.get("methods") or []:
            part = _build_function_part(
                method, class_name=cls_name, rel_path=rel_path,
                imports_list=imports_list,
                calls_map=calls_map, called_by_map=called_by_map,
            )
            if part is not None:
                parts.append(part)

    return parts


def _build_function_part(
    fn,
    class_name: str | None,
    rel_path: str,
    imports_list: list[str],
    calls_map: dict,
    called_by_map: dict,
) -> tuple[str, dict] | None:
    if not isinstance(fn, dict):
        return None
    name = fn.get("name") or ""
    if not name:
        return None
    key = f"{class_name}.{name}" if class_name else name
    callees = list(calls_map.get(key) or [])
    callers = list(called_by_map.get(key) or [])
    sig = fn.get("signature") or name
    lines = [f"# llm-tldr function: {key} (in {rel_path})"]
    lines.append(f"Signature: {sig}")
    if fn.get("is_async"):
        lines.append("Async: True")
    docstring = fn.get("docstring")
    if docstring:
        lines.append("")
        lines.append(docstring.strip())
    if callees:
        lines.append("")
        lines.append(f"Calls: {', '.join(callees)}")
    if callers:
        lines.append(f"Called by: {', '.join(callers)}")
    text = "\n".join(lines)
    payload = {
        "tldr_chunk_kind": "function",
        "tldr_function_name": name,
        "tldr_class_name": class_name,
        "tldr_callees": callees or None,
        "tldr_callers": callers or None,
        "tldr_caller_count": len(callers),
        "tldr_callee_count": len(callees),
        "tldr_imports": imports_list or None,
    }
    return (text, payload)


def _normalize_imports(imports) -> list[str]:
    """Reduce llm-tldr's import entries to a flat list of module names."""
    out: list[str] = []
    for imp in imports:
        if isinstance(imp, dict):
            mod = imp.get("module") or imp.get("name")
            if mod:
                out.append(str(mod))
        elif imp:
            out.append(str(imp))
    return out


def _hash_file(path: Path) -> str:
    """SHA-256 hash of the file's raw bytes, salted with the analysis
    format version. A format-version bump invalidates all stored hashes
    so the next sync re-extracts every file into the new format.
    """
    sha = hashlib.sha256()
    sha.update(ANALYSIS_FORMAT_VERSION.encode("ascii"))
    sha.update(b"\0")
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    return sha.hexdigest()


def get_llm_tldr_indexer() -> LlmTldrIndexer:
    return LlmTldrIndexer()
