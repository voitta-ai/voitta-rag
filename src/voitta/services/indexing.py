"""Indexing service for processing and storing document embeddings."""

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db.models import FolderIndexStatus, IndexedFile
from .chunking import ChunkingService, get_chunking_service
from .embedding import EmbeddingService, get_embedding_service
from .parsers import can_parse, parse_file, get_parser
from .parsers.pdf_parser import PdfParser, get_pdf_page_count
from .sparse_embedding import SparseEmbeddingService, get_sparse_embedding_service
from .vector_store import ChunkMetadata, VectorStoreService, get_vector_store

logger = logging.getLogger(__name__)

# Set up file logging for indexing
LOG_FILE = Path(__file__).parent.parent.parent.parent / "logs" / "indexing.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

idx_logger = logging.getLogger("voitta.indexing")
idx_logger.setLevel(logging.DEBUG)

for handler in idx_logger.handlers[:]:
    idx_logger.removeHandler(handler)

_file_handler = logging.FileHandler(LOG_FILE, mode='w', encoding='utf-8')
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(logging.Formatter(
    '%(asctime)s.%(msecs)03d | %(levelname)-7s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))
idx_logger.addHandler(_file_handler)


def compute_file_hash(file_path: Path) -> str:
    """Compute SHA-256 hash of a file's contents."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def _iso_to_epoch(iso_str: str) -> int | None:
    """Parse an ISO 8601 string to Unix epoch (seconds). Returns None on failure."""
    if not iso_str:
        return None
    try:
        # Handle various ISO formats (with/without timezone, fractional seconds)
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp())
    except (ValueError, OSError):
        return None


def _load_source_timestamps(
    file_path: str, abs_path: Path
) -> tuple[int | None, int | None]:
    """Load source timestamps for a file.

    Walks up directories to find .voitta_timestamps.json sidecar.
    Falls back to filesystem stat() for local (non-synced) files.

    Returns:
        (source_created_at, source_modified_at) as Unix epoch integers
    """
    # Walk up from the file's directory looking for the sidecar
    current = abs_path.parent
    while True:
        sidecar = current / ".voitta_timestamps.json"
        if sidecar.exists():
            try:
                data = json.loads(sidecar.read_text())
                # file_path is relative to root_path; we need the relative path
                # from the sidecar's directory
                # The sidecar uses remote_path keys which match the relative path
                # from the sync root (where the sidecar lives)
                rel_from_sidecar = str(abs_path.relative_to(current))
                entry = data.get(rel_from_sidecar, {})
                if entry:
                    created = _iso_to_epoch(entry.get("created_at", ""))
                    modified = _iso_to_epoch(entry.get("modified_at", ""))
                    return created, modified
            except Exception:
                pass
            break  # Found sidecar but no entry; don't walk further up
        parent = current.parent
        if parent == current:
            break
        current = parent

    # Fallback: filesystem stat()
    try:
        st = abs_path.stat()
        modified = int(st.st_mtime)
        # st_birthtime on macOS, st_ctime on Linux (inode change time)
        created = int(getattr(st, "st_birthtime", st.st_ctime))
        return created, modified
    except OSError:
        return None, None


class IndexingService:
    """Service for indexing documents into the vector store."""

    def __init__(
        self,
        chunker: ChunkingService | None = None,
        embedder: EmbeddingService | None = None,
        vector_store: VectorStoreService | None = None,
    ):
        self.settings = get_settings()
        self.chunker = chunker or get_chunking_service()
        self.embedder = embedder or get_embedding_service()
        self.sparse_embedder = get_sparse_embedding_service()
        self.vector_store = vector_store or get_vector_store()
        self.root_path = self.settings.root_path

    def index_file(
        self,
        file_path: str,
        folder_path: str,
        index_folder: str,
        db: Session,
        force: bool = False,
    ) -> tuple[bool, int]:
        """Index a single file.

        For PDFs, uses bucketed processing - parses in buckets (50-page splits)
        and stores each bucket's text chunks immediately for better progress feedback.

        Args:
            file_path: Relative path to the file
            folder_path: Relative path to the containing folder
            index_folder: Relative path to the folder at which indexing was triggered
            db: Database session
            force: If True, re-index even if unchanged

        Returns:
            Tuple of (was_indexed, chunk_count)
        """
        abs_path = self.root_path / file_path

        if not abs_path.exists():
            logger.warning(f"File not found: {file_path}")
            return False, 0

        if not can_parse(abs_path):
            logger.debug(f"Skipping unsupported file: {file_path}")
            return False, 0

        # Compute file hash
        file_hash = compute_file_hash(abs_path)
        file_size = abs_path.stat().st_size

        # Check if file has changed
        result = db.execute(
            select(IndexedFile).where(IndexedFile.file_path == file_path)
        )
        existing = result.scalar_one_or_none()

        # Check if we should skip indexing
        # Convention: negative chunk_count = in progress, positive = complete
        if not force:
            qdrant_count = self.vector_store.count_by_file(file_path)

            if existing and existing.content_hash == file_hash:
                # Hash matches - check completion status
                if existing.chunk_count < 0:
                    # Negative = was in progress, interrupted
                    idx_logger.info(
                        f"[INDEX] Incomplete (was in progress with {-existing.chunk_count} chunks), "
                        f"re-indexing: {file_path}"
                    )
                elif existing.chunk_count > 0 and existing.chunk_count == qdrant_count:
                    # Positive and matches Qdrant - complete
                    # For PDFs, also verify page count matches (detect changed PDFs)
                    if abs_path.suffix.lower() == '.pdf':
                        stored_page_count = self.vector_store.get_stored_page_count(file_path)
                        actual_page_count = get_pdf_page_count(abs_path)
                        if stored_page_count and actual_page_count > 0 and stored_page_count != actual_page_count:
                            idx_logger.info(
                                f"[INDEX] PDF page count changed: stored {stored_page_count}, "
                                f"actual {actual_page_count}, re-indexing: {file_path}"
                            )
                        else:
                            logger.debug(f"File unchanged, skipping: {file_path}")
                            return False, qdrant_count
                    else:
                        logger.debug(f"File unchanged, skipping: {file_path}")
                        return False, qdrant_count
                elif qdrant_count == 0:
                    idx_logger.info(f"[INDEX] File unchanged but missing from Qdrant, re-indexing: {file_path}")
                elif existing.chunk_count != qdrant_count:
                    idx_logger.info(
                        f"[INDEX] Chunk count mismatch (SQLite={existing.chunk_count}, Qdrant={qdrant_count}), "
                        f"re-indexing: {file_path}"
                    )
            elif not existing:
                # No SQLite record = never started or very old, must index
                if qdrant_count > 0:
                    idx_logger.info(f"[INDEX] No DB record, clearing {qdrant_count} orphan chunks: {file_path}")
            # Proceed to index

        idx_logger.info(f"[INDEX] Starting indexing: {file_path}")

        # DELETE existing chunks BEFORE parsing (so we don't have stale data)
        idx_logger.info(f"[INDEX] Deleting any existing chunks for: {file_path}")
        try:
            deleted = self.vector_store.delete_by_file(file_path)
            if deleted > 0:
                idx_logger.info(f"[INDEX] Deleted {deleted} existing chunks")
        except Exception as e:
            idx_logger.exception(f"[INDEX] EXCEPTION deleting chunks: {e}")

        # Check if this is a PDF for bucketed processing
        is_pdf = abs_path.suffix.lower() == '.pdf'

        if is_pdf:
            return self._index_pdf_bucketed(
                abs_path, file_path, folder_path, index_folder,
                file_hash, file_size, existing, db
            )
        else:
            return self._index_file_standard(
                abs_path, file_path, folder_path, index_folder,
                file_hash, file_size, existing, db
            )

    def _index_pdf_bucketed(
        self,
        abs_path: Path,
        file_path: str,
        folder_path: str,
        index_folder: str,
        file_hash: str,
        file_size: int,
        existing,
        db: Session,
    ) -> tuple[bool, int]:
        """Index a PDF using bucketed processing - parse and store incrementally."""
        idx_logger.info(f"[INDEX] Using BUCKETED PDF processing for: {file_path}")

        # Load source timestamps
        source_created_at, source_modified_at = _load_source_timestamps(file_path, abs_path)

        parser = PdfParser()
        file_name = abs_path.name
        indexed_at = datetime.now(timezone.utc).isoformat()
        total_chunks_stored = 0
        chunk_offset = 0  # Track chunk numbering across PDF buckets

        # Create/update SQLite record at START with negative chunk_count (in progress indicator)
        # Convention: negative = in progress, positive = complete
        try:
            if existing:
                existing.content_hash = file_hash
                existing.file_size = file_size
                existing.chunk_count = -1  # Negative = in progress
                existing.index_folder = index_folder
                existing.source_created_at = source_created_at
                existing.source_modified_at = source_modified_at
                existing.updated_at = datetime.now(timezone.utc)
                db_record = existing
            else:
                db_record = IndexedFile(
                    file_path=file_path,
                    folder_path=folder_path,
                    index_folder=index_folder,
                    content_hash=file_hash,
                    file_size=file_size,
                    chunk_count=-1,  # Negative = in progress
                    source_created_at=source_created_at,
                    source_modified_at=source_modified_at,
                )
                db.add(db_record)
            db.commit()
            idx_logger.info(f"[INDEX] Created DB record (in progress): {file_path}")
        except Exception as e:
            idx_logger.exception(f"[INDEX] EXCEPTION creating DB record: {e}")
            db.rollback()

        try:
            for parse_result in parser.parse_in_buckets(abs_path):
                if not parse_result.success:
                    idx_logger.error(f"[INDEX] Parse bucket FAILED: {parse_result.error}")
                    continue

                if not parse_result.content.strip():
                    idx_logger.warning(f"[INDEX] Empty content in bucket")
                    continue

                bucket_meta = parse_result.metadata or {}
                pdf_bucket_idx = bucket_meta.get('bucket_index', 0)
                total_pdf_buckets = bucket_meta.get('total_buckets', 1)

                idx_logger.info(
                    f"[INDEX] Processing PDF bucket {pdf_bucket_idx + 1}/{total_pdf_buckets}: "
                    f"{len(parse_result.content)} chars"
                )

                # Chunk this portion of content
                try:
                    text_chunks = self.chunker.chunk_text(parse_result.content)
                except Exception as e:
                    idx_logger.exception(f"[INDEX] EXCEPTION chunking: {e}")
                    continue

                if not text_chunks:
                    idx_logger.warning(f"[INDEX] No text chunks from PDF bucket {pdf_bucket_idx + 1}")
                    continue

                idx_logger.info(f"[INDEX] Generated {len(text_chunks)} text chunks")

                # Generate embeddings
                try:
                    texts = [c.text for c in text_chunks]
                    embeddings = self.embedder.embed_texts(texts)
                except Exception as e:
                    idx_logger.exception(f"[INDEX] EXCEPTION embedding: {e}")
                    continue

                # Extract page info from PDF bucket metadata
                start_page = bucket_meta.get('start_page')
                end_page = bucket_meta.get('end_page')
                source_page_count = bucket_meta.get('source_page_count')

                # Prepare and store
                chunk_data = []
                for i, (chunk, embedding) in enumerate(zip(text_chunks, embeddings)):
                    metadata = ChunkMetadata(
                        file_path=file_path,
                        folder_path=folder_path,
                        index_folder=index_folder,
                        file_name=file_name,
                        chunk_index=chunk_offset + i,
                        total_chunks=0,  # Will update at end
                        start_char=chunk.start_char,
                        end_char=chunk.end_char,
                        indexed_at=indexed_at,
                        start_page=start_page,
                        end_page=end_page,
                        source_page_count=source_page_count,
                        source_created_at=source_created_at,
                        source_modified_at=source_modified_at,
                    )
                    chunk_data.append((chunk.text, embedding, metadata))

                # Generate sparse embeddings
                sparse_vectors = self.sparse_embedder.embed_texts(
                    [chunk.text for chunk in text_chunks]
                )

                # Store immediately
                try:
                    self.vector_store.store_chunks(chunk_data, sparse_vectors=sparse_vectors)
                    total_chunks_stored += len(chunk_data)
                    chunk_offset += len(text_chunks)

                    # Update SQLite with progress (negative = in progress)
                    db_record.chunk_count = -total_chunks_stored
                    db_record.updated_at = datetime.now(timezone.utc)
                    db.commit()

                    idx_logger.info(
                        f"[INDEX] Stored {len(chunk_data)} chunks "
                        f"(total: {total_chunks_stored}) for PDF bucket {pdf_bucket_idx + 1}"
                    )
                except Exception as e:
                    idx_logger.exception(f"[INDEX] EXCEPTION storing: {e}")
                    db.rollback()
                    continue

        except Exception as e:
            idx_logger.exception(f"[INDEX] EXCEPTION in bucketed parsing: {e}")
            if total_chunks_stored == 0:
                return False, 0

        if total_chunks_stored == 0:
            idx_logger.error(f"[INDEX] No chunks stored for: {file_path}")
            return False, 0

        # Mark as complete (positive chunk_count)
        try:
            db_record.chunk_count = total_chunks_stored  # Positive = complete
            db_record.updated_at = datetime.now(timezone.utc)
            db.commit()
            idx_logger.info(f"[INDEX] SUCCESS: Indexed {total_chunks_stored} chunks for: {file_path}")
        except Exception as e:
            idx_logger.exception(f"[INDEX] EXCEPTION finalizing DB record: {e}")
            db.rollback()

        return True, total_chunks_stored

    def _index_file_standard(
        self,
        abs_path: Path,
        file_path: str,
        folder_path: str,
        index_folder: str,
        file_hash: str,
        file_size: int,
        existing,
        db: Session,
    ) -> tuple[bool, int]:
        """Standard indexing for non-PDF files."""
        idx_logger.info(f"[INDEX] Using STANDARD processing for: {file_path}")

        # Load source timestamps
        source_created_at, source_modified_at = _load_source_timestamps(file_path, abs_path)

        # Parse the file
        try:
            parse_result = parse_file(abs_path)
        except Exception as e:
            idx_logger.exception(f"[INDEX] EXCEPTION during parsing: {e}")
            return False, 0

        if not parse_result.success:
            idx_logger.error(f"[INDEX] Parse FAILED: {parse_result.error}")
            return False, 0

        idx_logger.info(f"[INDEX] Parse SUCCESS: {len(parse_result.content)} chars")

        if not parse_result.content.strip():
            idx_logger.warning(f"[INDEX] Empty content after parsing")
            return False, 0

        # Chunk the content
        try:
            chunks = self.chunker.chunk_text(parse_result.content)
        except Exception as e:
            idx_logger.exception(f"[INDEX] EXCEPTION during chunking: {e}")
            return False, 0

        if not chunks:
            idx_logger.warning(f"[INDEX] No chunks generated")
            return False, 0

        idx_logger.info(f"[INDEX] Generated {len(chunks)} chunks")

        # Generate embeddings (dense + sparse)
        try:
            texts = [chunk.text for chunk in chunks]
            embeddings = self.embedder.embed_texts(texts)
            sparse_vectors = self.sparse_embedder.embed_texts(texts)
        except Exception as e:
            idx_logger.exception(f"[INDEX] EXCEPTION during embedding: {e}")
            return False, 0

        # Prepare metadata
        file_name = abs_path.name
        indexed_at = datetime.now(timezone.utc).isoformat()

        chunk_data = []
        for chunk, embedding in zip(chunks, embeddings):
            metadata = ChunkMetadata(
                file_path=file_path,
                folder_path=folder_path,
                index_folder=index_folder,
                file_name=file_name,
                chunk_index=chunk.index,
                total_chunks=len(chunks),
                start_char=chunk.start_char,
                end_char=chunk.end_char,
                indexed_at=indexed_at,
                source_created_at=source_created_at,
                source_modified_at=source_modified_at,
            )
            chunk_data.append((chunk.text, embedding, metadata))

        # Store in vector database
        try:
            self.vector_store.store_chunks(chunk_data, sparse_vectors=sparse_vectors)
        except Exception as e:
            idx_logger.exception(f"[INDEX] EXCEPTION storing chunks: {e}")
            return False, 0

        # Update database record
        try:
            if existing:
                existing.content_hash = file_hash
                existing.file_size = file_size
                existing.chunk_count = len(chunks)
                existing.index_folder = index_folder
                existing.source_created_at = source_created_at
                existing.source_modified_at = source_modified_at
                existing.updated_at = datetime.now(timezone.utc)
            else:
                indexed_file = IndexedFile(
                    file_path=file_path,
                    folder_path=folder_path,
                    index_folder=index_folder,
                    content_hash=file_hash,
                    file_size=file_size,
                    chunk_count=len(chunks),
                    source_created_at=source_created_at,
                    source_modified_at=source_modified_at,
                )
                db.add(indexed_file)

            db.commit()  # Commit immediately - don't wait for end of folder
            idx_logger.info(f"[INDEX] SUCCESS: Indexed {len(chunks)} chunks")
        except Exception as e:
            idx_logger.exception(f"[INDEX] EXCEPTION updating database: {e}")
            db.rollback()
            return False, 0

        return True, len(chunks)

    def index_folder(
        self,
        folder_path: str,
        db: Session,
        force: bool = False,
    ) -> tuple[int, int, int]:
        """Index all supported files in a folder recursively.

        Args:
            folder_path: Relative path to the folder
            db: Database session
            force: If True, re-index all files

        Returns:
            Tuple of (files_indexed, total_chunks, files_skipped)
        """
        abs_path = self.root_path / folder_path if folder_path else self.root_path

        if not abs_path.exists():
            logger.error(f"Folder not found: {folder_path}")
            return 0, 0, 0

        # Update status to indexing
        result = db.execute(
            select(FolderIndexStatus).where(FolderIndexStatus.folder_path == folder_path)
        )
        status = result.scalar_one_or_none()
        if status:
            status.status = "indexing"
            status.error_message = None
            db.flush()

        files_indexed = 0
        total_chunks = 0
        files_skipped = 0

        # The index_folder is the folder at which indexing was triggered
        index_folder = folder_path

        try:
            # Collect all valid files first
            files_to_index = []
            for file_entry in abs_path.rglob("*"):
                if file_entry.is_file() and not file_entry.name.startswith("."):
                    # Skip files in hidden directories
                    if any(part.startswith(".") for part in file_entry.relative_to(abs_path).parts):
                        continue
                    files_to_index.append(file_entry)

            # Sort by file size (smallest first) for faster initial feedback
            files_to_index.sort(key=lambda f: f.stat().st_size)

            idx_logger.info(f"[INDEX] Found {len(files_to_index)} files to index in {folder_path}, sorted by size")

            # Process files in order
            for file_entry in files_to_index:
                file_rel_path = str(file_entry.relative_to(self.root_path))
                # The folder_path is the actual containing folder of the file
                file_folder_path = str(file_entry.parent.relative_to(self.root_path))

                was_indexed, chunk_count = self.index_file(
                    file_rel_path,
                    file_folder_path,
                    index_folder,
                    db,
                    force=force,
                )

                if was_indexed:
                    files_indexed += 1
                    total_chunks += chunk_count
                else:
                    files_skipped += 1

            # Update status to indexed — but respect "pending" set by
            # another session during indexing (e.g. Anamnesis memory update)
            if status:
                db.refresh(status)
                if status.status != "pending":
                    status.status = "indexed"
                    status.indexed_at = datetime.now(timezone.utc)
                db.flush()

            logger.info(
                f"Folder '{folder_path}' indexed (recursive): "
                f"{files_indexed} files, {total_chunks} chunks, {files_skipped} skipped"
            )

        except Exception as e:
            logger.exception(f"Error indexing folder '{folder_path}': {e}")
            if status:
                status.status = "error"
                status.error_message = str(e)
                db.flush()
            raise

        return files_indexed, total_chunks, files_skipped

    def remove_file_index(self, file_path: str, db: Session) -> bool:
        """Remove a file's index from the vector store and database.

        Args:
            file_path: Relative path to the file
            db: Database session

        Returns:
            True if file was removed, False if not found
        """
        # Delete from vector store
        deleted_count = self.vector_store.delete_by_file(file_path)

        # Delete from database
        result = db.execute(
            select(IndexedFile).where(IndexedFile.file_path == file_path)
        )
        indexed_file = result.scalar_one_or_none()

        if indexed_file:
            db.delete(indexed_file)
            db.flush()
            logger.info(f"Removed index for file: {file_path}")
            return True

        return deleted_count > 0

    def disable_folder_index(self, folder_path: str, db: Session) -> bool:
        """Disable a folder's index without deleting chunks.

        Chunks are preserved but excluded from MCP searches.
        Use sync_folder when re-enabling to reconcile files and chunks.

        Args:
            folder_path: Relative path to the folder (the index_folder)
            db: Database session

        Returns:
            True if folder was disabled, False if not found
        """
        result = db.execute(
            select(FolderIndexStatus).where(FolderIndexStatus.folder_path == folder_path)
        )
        status = result.scalar_one_or_none()

        if status:
            status.status = "disabled"
            db.flush()
            logger.info(f"Disabled folder index: {folder_path}")
            return True

        logger.warning(f"Folder not found for disabling: {folder_path}")
        return False

    def enable_folder_index(self, folder_path: str, db: Session) -> bool:
        """Re-enable a disabled folder's index.

        Sets status back to 'indexed'. Call sync_folder after this
        to reconcile files and chunks.

        Args:
            folder_path: Relative path to the folder (the index_folder)
            db: Database session

        Returns:
            True if folder was enabled, False if not found
        """
        result = db.execute(
            select(FolderIndexStatus).where(FolderIndexStatus.folder_path == folder_path)
        )
        status = result.scalar_one_or_none()

        if status:
            status.status = "indexed"
            db.flush()
            logger.info(f"Enabled folder index: {folder_path}")
            return True

        logger.warning(f"Folder not found for enabling: {folder_path}")
        return False

    def sync_folder(
        self,
        folder_path: str,
        db: Session,
    ) -> tuple[int, int, int]:
        """Sync a folder's index with the actual files on disk.

        This reconciles the index with the filesystem:
        - If a file is missing from disk → delete its chunks
        - If a file exists but is not indexed → index it
        - If a file exists and is indexed but changed → re-index it

        Args:
            folder_path: Relative path to the folder (the index_folder)
            db: Database session

        Returns:
            Tuple of (files_added, files_removed, files_unchanged)
        """
        abs_path = self.root_path / folder_path if folder_path else self.root_path

        if not abs_path.exists():
            logger.error(f"Folder not found: {folder_path}")
            return 0, 0, 0

        files_added = 0
        files_removed = 0
        files_unchanged = 0

        # Get all indexed files for this index_folder
        result = db.execute(
            select(IndexedFile).where(IndexedFile.index_folder == folder_path)
        )
        indexed_files = {f.file_path: f for f in result.scalars().all()}

        # Get all actual files on disk (recursively)
        actual_files: set[str] = set()
        for file_entry in abs_path.rglob("*"):
            if file_entry.is_file() and not file_entry.name.startswith("."):
                # Skip files in hidden directories
                if any(part.startswith(".") for part in file_entry.relative_to(abs_path).parts):
                    continue
                if can_parse(file_entry):
                    file_rel_path = str(file_entry.relative_to(self.root_path))
                    actual_files.add(file_rel_path)

        # Find files to remove (indexed but no longer on disk)
        indexed_paths = set(indexed_files.keys())
        files_to_remove = indexed_paths - actual_files

        for file_path in files_to_remove:
            self.remove_file_index(file_path, db)
            files_removed += 1
            logger.info(f"Removed missing file from index: {file_path}")

        # Find files to add or update (on disk but not indexed, or changed)
        for file_rel_path in actual_files:
            file_entry = self.root_path / file_rel_path
            file_folder_path = str(file_entry.parent.relative_to(self.root_path))

            # Check if file has chunks in Qdrant
            chunk_count = self.vector_store.count_by_file(file_rel_path)
            is_indexed = chunk_count > 0

            if is_indexed:
                # Check if file changed (hash check)
                need_reindex = False
                if file_rel_path in indexed_files:
                    existing = indexed_files[file_rel_path]
                    current_hash = compute_file_hash(file_entry)
                    if existing.content_hash != current_hash:
                        need_reindex = True
                        idx_logger.info(f"[SYNC] File hash changed: {file_rel_path}")

                # For PDFs, also check page count
                if not need_reindex and file_entry.suffix.lower() == '.pdf':
                    stored_page_count = self.vector_store.get_stored_page_count(file_rel_path)
                    actual_page_count = get_pdf_page_count(file_entry)
                    if stored_page_count is not None and actual_page_count > 0:
                        if stored_page_count != actual_page_count:
                            need_reindex = True
                            idx_logger.info(
                                f"[SYNC] PDF page count mismatch for {file_rel_path}: "
                                f"stored={stored_page_count}, actual={actual_page_count}"
                            )
                    elif stored_page_count is None and actual_page_count > 0:
                        # Page count not stored, backfill by re-indexing
                        need_reindex = True
                        idx_logger.info(
                            f"[SYNC] PDF missing page count, backfilling: {file_rel_path}"
                        )

                if need_reindex:
                    was_indexed, _ = self.index_file(
                        file_rel_path, file_folder_path, folder_path, db, force=True
                    )
                    if was_indexed:
                        files_added += 1  # Count as added since content changed
                else:
                    files_unchanged += 1
            else:
                # New file or no chunks, index it
                was_indexed, _ = self.index_file(
                    file_rel_path, file_folder_path, folder_path, db
                )
                if was_indexed:
                    files_added += 1

        logger.info(
            f"Folder '{folder_path}' synced: "
            f"{files_added} added/updated, {files_removed} removed, {files_unchanged} unchanged"
        )

        return files_added, files_removed, files_unchanged

    def remove_folder_index(self, folder_path: str, db: Session) -> int:
        """DEPRECATED: Use disable_folder_index instead.

        Actually removes all file indexes for files that were indexed from a folder.
        Only use this for permanent deletion.

        Args:
            folder_path: Relative path to the folder (the index_folder)
            db: Database session

        Returns:
            Number of files removed
        """
        # Delete from vector store using index_folder
        self.vector_store.delete_by_index_folder(folder_path)

        # Delete from database - files where index_folder matches
        result = db.execute(
            select(IndexedFile).where(IndexedFile.index_folder == folder_path)
        )
        indexed_files = result.scalars().all()

        count = len(indexed_files)
        for indexed_file in indexed_files:
            db.delete(indexed_file)

        # Also remove the folder status
        result = db.execute(
            select(FolderIndexStatus).where(FolderIndexStatus.folder_path == folder_path)
        )
        status = result.scalar_one_or_none()
        if status:
            db.delete(status)

        db.flush()
        logger.info(f"Permanently removed index for {count} files from index_folder: {folder_path}")

        return count


# Global singleton instance
_indexing_service: IndexingService | None = None


def get_indexing_service() -> IndexingService:
    """Get the global indexing service instance."""
    global _indexing_service
    if _indexing_service is None:
        _indexing_service = IndexingService()
    return _indexing_service
