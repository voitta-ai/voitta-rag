"""Indexing service for processing and storing document embeddings."""

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db.models import FolderIndexStatus, IndexedFile
from .chunking import ChunkingService, get_chunking_service
from .embedding import EmbeddingService, get_embedding_service
from .parsers import can_parse, parse_file
from .vector_store import ChunkMetadata, VectorStoreService, get_vector_store

logger = logging.getLogger(__name__)


def compute_file_hash(file_path: Path) -> str:
    """Compute SHA-256 hash of a file's contents."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


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

        if existing and not force:
            if existing.content_hash == file_hash:
                logger.debug(f"File unchanged, skipping: {file_path}")
                return False, existing.chunk_count

        # Parse the file
        logger.info(f"Parsing file: {file_path}")
        parse_result = parse_file(abs_path)

        if not parse_result.success:
            logger.error(f"Failed to parse {file_path}: {parse_result.error}")
            return False, 0

        if not parse_result.content.strip():
            logger.warning(f"Empty content after parsing: {file_path}")
            return False, 0

        # Delete existing chunks if re-indexing
        if existing:
            self.vector_store.delete_by_file(file_path)

        # Chunk the content
        chunks = self.chunker.chunk_text(parse_result.content)

        if not chunks:
            logger.warning(f"No chunks generated for: {file_path}")
            return False, 0

        logger.info(f"Generated {len(chunks)} chunks for: {file_path}")

        # Generate embeddings
        texts = [chunk.text for chunk in chunks]
        embeddings = self.embedder.embed_texts(texts)

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
            )
            chunk_data.append((chunk.text, embedding, metadata))

        # Store in vector database
        self.vector_store.store_chunks(chunk_data)

        # Update database record
        if existing:
            existing.content_hash = file_hash
            existing.file_size = file_size
            existing.chunk_count = len(chunks)
            existing.index_folder = index_folder
            existing.updated_at = datetime.now(timezone.utc)
        else:
            indexed_file = IndexedFile(
                file_path=file_path,
                folder_path=folder_path,
                index_folder=index_folder,
                content_hash=file_hash,
                file_size=file_size,
                chunk_count=len(chunks),
            )
            db.add(indexed_file)

        db.flush()
        logger.info(f"Indexed {len(chunks)} chunks for: {file_path}")

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
            # Recursively find all files in folder and subfolders
            for file_entry in abs_path.rglob("*"):
                if file_entry.is_file() and not file_entry.name.startswith("."):
                    # Skip files in hidden directories
                    if any(part.startswith(".") for part in file_entry.relative_to(abs_path).parts):
                        continue

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

            # Update status to indexed
            if status:
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

            if file_rel_path in indexed_files:
                # Check if file changed
                existing = indexed_files[file_rel_path]
                current_hash = compute_file_hash(file_entry)
                if existing.content_hash != current_hash:
                    # File changed, re-index
                    was_indexed, _ = self.index_file(
                        file_rel_path, file_folder_path, folder_path, db, force=True
                    )
                    if was_indexed:
                        files_added += 1  # Count as added since content changed
                else:
                    files_unchanged += 1
            else:
                # New file, index it
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
