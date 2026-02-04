"""Vector store service using Qdrant."""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels
from qdrant_client.http.exceptions import UnexpectedResponse

from ..config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class ChunkMetadata:
    """Metadata for a stored chunk."""

    file_path: str
    folder_path: str  # The folder containing the file
    index_folder: str  # The folder at which indexing was triggered
    file_name: str
    chunk_index: int
    total_chunks: int
    start_char: int
    end_char: int
    indexed_at: str  # ISO format
    # PDF-specific fields (None for non-PDF files)
    start_page: int | None = None  # First page this chunk came from
    end_page: int | None = None  # Last page this chunk came from
    source_page_count: int | None = None  # Total pages in source PDF


@dataclass
class StoredChunk:
    """A chunk stored in the vector database."""

    id: str
    text: str
    metadata: ChunkMetadata
    score: float | None = None


class VectorStoreService:
    """Service for storing and retrieving document chunks in Qdrant."""

    def __init__(self):
        settings = get_settings()
        self.host = settings.qdrant_host
        self.port = settings.qdrant_port
        self.collection_name = settings.qdrant_collection
        self.dimension = settings.embedding_dimension
        self._client: QdrantClient | None = None

    @property
    def client(self) -> QdrantClient:
        """Lazy load the Qdrant client."""
        if self._client is None:
            logger.info(f"Connecting to Qdrant at {self.host}:{self.port}")
            self._client = QdrantClient(host=self.host, port=self.port)
            self._ensure_collection()
        return self._client

    def _ensure_collection(self) -> None:
        """Ensure the collection exists with proper configuration."""
        try:
            self._client.get_collection(self.collection_name)
            logger.info(f"Collection '{self.collection_name}' exists")
        except (UnexpectedResponse, Exception):
            logger.info(f"Creating collection '{self.collection_name}'")
            self._client.create_collection(
                collection_name=self.collection_name,
                vectors_config=qmodels.VectorParams(
                    size=self.dimension,
                    distance=qmodels.Distance.COSINE,
                ),
            )
            # Create payload indexes for efficient filtering
            self._client.create_payload_index(
                collection_name=self.collection_name,
                field_name="file_path",
                field_schema=qmodels.PayloadSchemaType.KEYWORD,
            )
            self._client.create_payload_index(
                collection_name=self.collection_name,
                field_name="folder_path",
                field_schema=qmodels.PayloadSchemaType.KEYWORD,
            )
            self._client.create_payload_index(
                collection_name=self.collection_name,
                field_name="index_folder",
                field_schema=qmodels.PayloadSchemaType.KEYWORD,
            )
            logger.info(f"Collection '{self.collection_name}' created")

    def store_chunks(
        self,
        chunks: list[tuple[str, list[float], ChunkMetadata]],
        batch_size: int = 100,
    ) -> list[str]:
        """Store multiple chunks with their embeddings.

        Args:
            chunks: List of (text, embedding, metadata) tuples
            batch_size: Number of chunks per batch to avoid payload limits

        Returns:
            List of generated point IDs
        """
        if not chunks:
            return []

        points = []
        ids = []

        for text, embedding, metadata in chunks:
            point_id = str(uuid.uuid4())
            ids.append(point_id)

            payload = {
                "text": text,
                "file_path": metadata.file_path,
                "folder_path": metadata.folder_path,
                "index_folder": metadata.index_folder,
                "file_name": metadata.file_name,
                "chunk_index": metadata.chunk_index,
                "total_chunks": metadata.total_chunks,
                "start_char": metadata.start_char,
                "end_char": metadata.end_char,
                "indexed_at": metadata.indexed_at,
            }
            # Add PDF-specific fields if present
            if metadata.start_page is not None:
                payload["start_page"] = metadata.start_page
            if metadata.end_page is not None:
                payload["end_page"] = metadata.end_page
            if metadata.source_page_count is not None:
                payload["source_page_count"] = metadata.source_page_count

            points.append(
                qmodels.PointStruct(
                    id=point_id,
                    vector=embedding,
                    payload=payload,
                )
            )

        # Store in batches to avoid Qdrant payload size limits
        for i in range(0, len(points), batch_size):
            batch = points[i : i + batch_size]
            self.client.upsert(collection_name=self.collection_name, points=batch)

        logger.info(f"Stored {len(points)} chunks in Qdrant")

        return ids

    def delete_by_file(self, file_path: str) -> int:
        """Delete all chunks for a specific file.

        Returns:
            Number of deleted points
        """
        # First count how many we'll delete
        count_result = self.client.count(
            collection_name=self.collection_name,
            count_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="file_path",
                        match=qmodels.MatchValue(value=file_path),
                    )
                ]
            ),
        )
        count = count_result.count

        if count > 0:
            self.client.delete(
                collection_name=self.collection_name,
                points_selector=qmodels.FilterSelector(
                    filter=qmodels.Filter(
                        must=[
                            qmodels.FieldCondition(
                                key="file_path",
                                match=qmodels.MatchValue(value=file_path),
                            )
                        ]
                    )
                ),
            )
            logger.info(f"Deleted {count} chunks for file: {file_path}")

        return count

    def delete_by_folder(self, folder_path: str) -> int:
        """Delete all chunks for files in a specific folder.

        Returns:
            Number of deleted points
        """
        # First count how many we'll delete
        count_result = self.client.count(
            collection_name=self.collection_name,
            count_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="folder_path",
                        match=qmodels.MatchValue(value=folder_path),
                    )
                ]
            ),
        )
        count = count_result.count

        if count > 0:
            self.client.delete(
                collection_name=self.collection_name,
                points_selector=qmodels.FilterSelector(
                    filter=qmodels.Filter(
                        must=[
                            qmodels.FieldCondition(
                                key="folder_path",
                                match=qmodels.MatchValue(value=folder_path),
                            )
                        ]
                    )
                ),
            )
            logger.info(f"Deleted {count} chunks for folder: {folder_path}")

        return count

    def delete_by_index_folder(self, index_folder: str) -> int:
        """Delete all chunks that were indexed from a specific index folder.

        This deletes all chunks where the indexing was triggered from the given folder,
        including files in subfolders.

        Returns:
            Number of deleted points
        """
        # First count how many we'll delete
        count_result = self.client.count(
            collection_name=self.collection_name,
            count_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="index_folder",
                        match=qmodels.MatchValue(value=index_folder),
                    )
                ]
            ),
        )
        count = count_result.count

        if count > 0:
            self.client.delete(
                collection_name=self.collection_name,
                points_selector=qmodels.FilterSelector(
                    filter=qmodels.Filter(
                        must=[
                            qmodels.FieldCondition(
                                key="index_folder",
                                match=qmodels.MatchValue(value=index_folder),
                            )
                        ]
                    )
                ),
            )
            logger.info(f"Deleted {count} chunks for index_folder: {index_folder}")

        return count

    def search(
        self,
        query_embedding: list[float],
        limit: int = 10,
        folder_filter: str | None = None,
        include_folders: list[str] | None = None,
        exclude_folders: list[str] | None = None,
        exclude_index_folders: list[str] | None = None,
    ) -> list[StoredChunk]:
        """Search for similar chunks.

        Args:
            query_embedding: The query embedding vector
            limit: Maximum number of results
            folder_filter: Optional single folder path to filter by (legacy)
            include_folders: Optional list of folder paths to include (OR logic)
            exclude_folders: Optional list of folder paths to exclude
            exclude_index_folders: Optional list of index_folders to exclude (for disabled folders)

        Returns:
            List of matching chunks with scores
        """
        must_conditions = []
        must_not_conditions = []

        # Legacy single folder filter
        if folder_filter:
            must_conditions.append(
                qmodels.FieldCondition(
                    key="folder_path",
                    match=qmodels.MatchValue(value=folder_filter),
                )
            )

        # Include folders (OR logic - match any of these folders)
        if include_folders:
            must_conditions.append(
                qmodels.FieldCondition(
                    key="folder_path",
                    match=qmodels.MatchAny(any=include_folders),
                )
            )

        # Exclude folders by folder_path
        if exclude_folders:
            for folder in exclude_folders:
                must_not_conditions.append(
                    qmodels.FieldCondition(
                        key="folder_path",
                        match=qmodels.MatchValue(value=folder),
                    )
                )

        # Exclude folders by index_folder (for disabled folders)
        if exclude_index_folders:
            for folder in exclude_index_folders:
                must_not_conditions.append(
                    qmodels.FieldCondition(
                        key="index_folder",
                        match=qmodels.MatchValue(value=folder),
                    )
                )

        # Build filter
        search_filter = None
        if must_conditions or must_not_conditions:
            search_filter = qmodels.Filter(
                must=must_conditions if must_conditions else None,
                must_not=must_not_conditions if must_not_conditions else None,
            )

        results = self.client.query_points(
            collection_name=self.collection_name,
            query=query_embedding,
            limit=limit,
            query_filter=search_filter,
        ).points

        chunks = []
        for result in results:
            payload = result.payload
            chunks.append(
                StoredChunk(
                    id=str(result.id),
                    text=payload["text"],
                    metadata=ChunkMetadata(
                        file_path=payload["file_path"],
                        folder_path=payload["folder_path"],
                        index_folder=payload.get("index_folder", payload["folder_path"]),
                        file_name=payload["file_name"],
                        chunk_index=payload["chunk_index"],
                        total_chunks=payload["total_chunks"],
                        start_char=payload["start_char"],
                        end_char=payload["end_char"],
                        indexed_at=payload["indexed_at"],
                        start_page=payload.get("start_page"),
                        end_page=payload.get("end_page"),
                        source_page_count=payload.get("source_page_count"),
                    ),
                    score=result.score,
                )
            )

        return chunks

    def get_collection_info(self) -> dict:
        """Get information about the collection."""
        try:
            info = self.client.get_collection(self.collection_name)
            return {
                "name": self.collection_name,
                "vectors_count": info.vectors_count,
                "points_count": info.points_count,
                "status": info.status.value,
            }
        except Exception as e:
            return {"error": str(e)}

    def count_by_file(self, file_path: str) -> int:
        """Count chunks for a specific file."""
        try:
            result = self.client.count(
                collection_name=self.collection_name,
                count_filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="file_path",
                            match=qmodels.MatchValue(value=file_path),
                        )
                    ]
                ),
            )
            return result.count
        except Exception:
            return 0

    def get_stored_page_count(self, file_path: str) -> int | None:
        """Get the stored source_page_count for a PDF file.

        Returns None if no chunks exist or page count not stored.
        """
        try:
            # Get just one chunk to check the source_page_count
            results, _ = self.client.scroll(
                collection_name=self.collection_name,
                limit=1,
                scroll_filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="file_path",
                            match=qmodels.MatchValue(value=file_path),
                        )
                    ]
                ),
                with_payload=["source_page_count"],
                with_vectors=False,
            )

            if results and results[0].payload.get("source_page_count"):
                return results[0].payload["source_page_count"]
            return None
        except Exception as e:
            logger.error(f"Error getting stored page count for {file_path}: {e}")
            return None

    def get_chunks_by_range(
        self,
        file_path: str,
        first_chunk: int,
        last_chunk: int,
    ) -> list[StoredChunk]:
        """Get chunks for a file within a specified index range.

        Args:
            file_path: Path to the file
            first_chunk: First chunk index (inclusive, 0-based)
            last_chunk: Last chunk index (inclusive)

        Returns:
            List of chunks sorted by chunk_index
        """
        try:
            # Scroll through chunks for this file
            chunks = []
            offset = None

            while True:
                results, offset = self.client.scroll(
                    collection_name=self.collection_name,
                    limit=100,
                    offset=offset,
                    scroll_filter=qmodels.Filter(
                        must=[
                            qmodels.FieldCondition(
                                key="file_path",
                                match=qmodels.MatchValue(value=file_path),
                            )
                        ]
                    ),
                    with_payload=True,
                    with_vectors=False,
                )

                for point in results:
                    payload = point.payload
                    chunk_index = payload["chunk_index"]

                    # Filter by chunk range
                    if first_chunk <= chunk_index <= last_chunk:
                        chunks.append(
                            StoredChunk(
                                id=str(point.id),
                                text=payload["text"],
                                metadata=ChunkMetadata(
                                    file_path=payload["file_path"],
                                    folder_path=payload["folder_path"],
                                    index_folder=payload.get("index_folder", payload["folder_path"]),
                                    file_name=payload["file_name"],
                                    chunk_index=payload["chunk_index"],
                                    total_chunks=payload["total_chunks"],
                                    start_char=payload["start_char"],
                                    end_char=payload["end_char"],
                                    indexed_at=payload["indexed_at"],
                                    start_page=payload.get("start_page"),
                                    end_page=payload.get("end_page"),
                                    source_page_count=payload.get("source_page_count"),
                                ),
                                score=None,
                            )
                        )

                if offset is None:
                    break

            # Sort by chunk_index
            chunks.sort(key=lambda c: c.metadata.chunk_index)
            return chunks

        except Exception as e:
            logger.error(f"Error getting chunks by range for {file_path}: {e}")
            return []

    def get_file_chunk_counts(self, folder_prefix: str = "") -> dict[str, int]:
        """Get chunk counts for all files, optionally filtered by folder prefix.

        Args:
            folder_prefix: If provided, only return files whose file_path starts with this prefix

        Returns:
            Dict mapping file_path to chunk count
        """
        try:
            # Scroll through all points to get unique file paths with counts
            # This is more efficient than counting each file individually
            file_counts: dict[str, int] = {}

            # Use scroll to iterate through all points
            offset = None
            while True:
                results, offset = self.client.scroll(
                    collection_name=self.collection_name,
                    limit=1000,
                    offset=offset,
                    with_payload=["file_path"],
                    with_vectors=False,
                )

                for point in results:
                    file_path = point.payload.get("file_path", "")
                    if folder_prefix and not file_path.startswith(folder_prefix):
                        continue
                    file_counts[file_path] = file_counts.get(file_path, 0) + 1

                if offset is None:
                    break

            return file_counts
        except Exception as e:
            logger.error(f"Error getting file chunk counts: {e}")
            return {}


# Global singleton instance
_vector_store: VectorStoreService | None = None


def get_vector_store() -> VectorStoreService:
    """Get the global vector store service instance."""
    global _vector_store
    if _vector_store is None:
        _vector_store = VectorStoreService()
    return _vector_store
