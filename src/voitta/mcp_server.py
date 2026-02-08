"""MCP server for voitta-rag RAG capabilities."""

import logging
import mimetypes
from contextvars import ContextVar
from pathlib import Path

from fastmcp import FastMCP
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from .config import get_settings
from .db.database import get_sync_engine
from .db.models import FileMetadata, FolderIndexStatus, IndexedFile, User, UserFolderSetting
from .services.embedding import get_embedding_service
from .services.parsers import parse_file
from .services.sparse_embedding import get_sparse_embedding_service
from .services.vector_store import get_vector_store

logger = logging.getLogger(__name__)

# Context variable to store current user from X-User-Name header
current_user: ContextVar[str | None] = ContextVar("current_user", default=None)

# Initialize MCP server
mcp = FastMCP("voitta-rag")


class UserHeaderMiddleware(BaseHTTPMiddleware):
    """Middleware to extract X-User-Name header and store in context."""

    async def dispatch(self, request: Request, call_next):
        user_name = request.headers.get("X-User-Name")
        if user_name:
            print(f"🔑 MCP request from user: {user_name}", flush=True)
            current_user.set(user_name)
        else:
            print(f"🔑 MCP request (no user header) {request.method} {request.url.path}", flush=True)
            current_user.set(None)
        response = await call_next(request)
        return response



def _get_current_user_name() -> str | None:
    """Get the current user name from context, or None if not provided."""
    retval = current_user.get()
    return retval


def _get_or_create_user(db: Session, user_name: str) -> User:
    """Get existing user or create a new one (case-insensitive match)."""
    result = db.execute(select(User).where(func.lower(User.name) == user_name.lower()))
    user = result.scalar_one_or_none()
    if not user:
        user = User(name=user_name)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def _get_user_active_folders(db: Session, user_id: int) -> list[str]:
    """Get list of folder paths that are active for search for the user."""
    result = db.execute(
        select(UserFolderSetting.folder_path).where(
            UserFolderSetting.user_id == user_id,
            UserFolderSetting.search_active == True,  # noqa: E712
        )
    )
    return [row[0] for row in result.fetchall()]


class SearchResult(BaseModel):
    """A single search result."""

    text: str = Field(description="The text content of the chunk")
    score: float = Field(description="Similarity score (0-1, higher is better)")
    file_path: str = Field(description="Path to the source file")
    file_name: str = Field(description="Name of the source file")
    folder_path: str = Field(description="Folder containing the file")
    index_folder: str = Field(description="Folder at which indexing was triggered")
    chunk_index: int = Field(description="Index of this chunk within the file")
    total_chunks: int = Field(description="Total number of chunks in the file")
    file_metadata: str | None = Field(description="User-added metadata/notes for the file")


class IndexedFolderInfo(BaseModel):
    """Information about an indexed folder."""

    folder_path: str = Field(description="Path to the folder")
    status: str = Field(description="Index status: indexed, indexing, pending, disabled, error, none")
    file_count: int = Field(description="Number of indexed files in this folder")
    total_chunks: int = Field(description="Total chunks across all files")
    metadata: str | None = Field(description="User-added metadata/notes for the folder")


class FileContent(BaseModel):
    """Content of an indexed file."""

    file_path: str = Field(description="Path to the file")
    file_name: str = Field(description="Name of the file")
    content: str = Field(description="Full text content of the file")
    chunk_count: int = Field(description="Number of chunks this file was split into")
    metadata: str | None = Field(description="User-added metadata/notes for the file")


class ChunkRangeResult(BaseModel):
    """Result of getting a chunk range."""

    success: bool = Field(description="Whether the operation succeeded")
    file_path: str = Field(description="Path to the file")
    merged_text: str = Field(description="Merged text content with overlaps removed")
    first_chunk: int = Field(description="First chunk index requested")
    last_chunk: int = Field(description="Last chunk index requested")
    actual_first_chunk: int = Field(description="Actual first chunk index returned")
    actual_last_chunk: int = Field(description="Actual last chunk index returned")
    total_chunks_in_file: int = Field(description="Total number of chunks in the file")
    chunks_returned: int = Field(description="Number of chunks included in result")
    truncated_to_limit: bool = Field(description="True if result was truncated due to 20 chunk limit")
    truncated_beyond_file: bool = Field(description="True if last_chunk exceeded file's chunk count")
    error: str | None = Field(description="Error message if success is False")


class FileUriResult(BaseModel):
    """Result of getting a file URI for download."""

    uri: str = Field(description="Full URI to download the raw file (use with wget/curl)")
    file_path: str = Field(description="Path to the file (relative to root)")
    file_name: str = Field(description="Name of the file")
    size: int = Field(description="File size in bytes")
    mime_type: str = Field(description="MIME type of the file")


@mcp.tool()
def search(
    query: str,
    limit: int | None = None,
    include_folders: list[str] | None = None,
    exclude_folders: list[str] | None = None,
    sparse_weight: float | None = None,
) -> list[SearchResult]:
    """Search indexed documents using hybrid semantic + keyword similarity.

    Args:
        query: The search query text
        limit: Max results to return. Defaults to 20.
        include_folders: Folder paths to restrict search to
        exclude_folders: Folder paths to exclude
        sparse_weight: BM25 vs semantic balance: 0.0 = pure semantic, 1.0 = pure keyword. Defaults to 0.1.

    Returns:
        List of matching document chunks with metadata and similarity scores

    """
    user_name = _get_current_user_name()

    settings = get_settings()
    if limit is None:
        limit = settings.mcp_search_limit
    if sparse_weight is None:
        sparse_weight = settings.sparse_weight
    embedding_service = get_embedding_service()
    vector_store = get_vector_store()
    engine = get_sync_engine()

    effective_include_folders = include_folders
    disabled_index_folders = []

    with Session(engine) as db:
        if user_name:
            # User-based folder filtering
            user = _get_or_create_user(db, user_name)
            user_active_folders = _get_user_active_folders(db, user.id)

            if not user_active_folders:
                return []

            # Get all unique folder_path values from indexed files
            result = db.execute(select(IndexedFile.folder_path).distinct())
            all_indexed_folder_paths = [row[0] for row in result.fetchall()]

            # Expand active folders to include subfolders
            expanded_active_folders = set(user_active_folders)
            for folder_path in all_indexed_folder_paths:
                folder_normalized = folder_path.rstrip("/")
                for active_folder in user_active_folders:
                    active_normalized = active_folder.rstrip("/")
                    if folder_normalized == active_normalized or folder_normalized.startswith(active_normalized + "/"):
                        expanded_active_folders.add(folder_path)
                        break

            # Combine with explicit include_folders filter
            effective_include_folders = list(expanded_active_folders)
            if include_folders:
                filtered_folders = set()
                for f in effective_include_folders:
                    f_normalized = f.rstrip("/")
                    for requested in include_folders:
                        req_normalized = requested.rstrip("/")
                        if f_normalized == req_normalized or f_normalized.startswith(req_normalized + "/"):
                            filtered_folders.add(f)
                            break
                effective_include_folders = list(filtered_folders)
                if not effective_include_folders:
                    return []

        # Get disabled folders to exclude from search (by index_folder)
        result = db.execute(
            select(FolderIndexStatus.folder_path).where(FolderIndexStatus.status == "disabled")
        )
        disabled_index_folders = [row[0] for row in result.fetchall()]

    # Generate query embeddings (dense + sparse)
    query_embedding = embedding_service.embed_query(query)
    sparse_service = get_sparse_embedding_service()
    sparse_query = sparse_service.embed_query(query)

    # Search vector store with hybrid retrieval (excluding disabled index_folders)
    chunks = vector_store.search(
        query_embedding=query_embedding,
        limit=limit,
        include_folders=effective_include_folders,
        exclude_folders=exclude_folders,
        exclude_index_folders=disabled_index_folders if disabled_index_folders else None,
        sparse_query=sparse_query,
        sparse_weight=sparse_weight,
    )

    # Get file metadata from database
    file_paths = list(set(chunk.metadata.file_path for chunk in chunks))
    file_metadata_map = {}

    with Session(engine) as db:
        if file_paths:
            result = db.execute(
                select(FileMetadata).where(FileMetadata.path.in_(file_paths))
            )
            for meta in result.scalars().all():
                file_metadata_map[meta.path] = meta.metadata_text

    # Build results
    results = []
    for chunk in chunks:
        results.append(
            SearchResult(
                text=chunk.text,
                score=chunk.score or 0.0,
                file_path=chunk.metadata.file_path,
                file_name=chunk.metadata.file_name,
                folder_path=chunk.metadata.folder_path,
                index_folder=chunk.metadata.index_folder,
                chunk_index=chunk.metadata.chunk_index,
                total_chunks=chunk.metadata.total_chunks,
                file_metadata=file_metadata_map.get(chunk.metadata.file_path),
            )
        )

    return results


@mcp.tool()
def list_indexed_folders() -> list[IndexedFolderInfo]:
    """List all folders that have been indexed, with their status and metadata.

    Returns:
        List of indexed folders with status, file counts, and user metadata.
        If no X-User-Name header, returns all folders.
    """
    user_name = _get_current_user_name()
    engine = get_sync_engine()

    with Session(engine) as db:
        user_active_folders = None
        if user_name:
            user = _get_or_create_user(db, user_name)
            user_active_folders = _get_user_active_folders(db, user.id)
            if not user_active_folders:
                return []

        # Get all folder index statuses
        result = db.execute(select(FolderIndexStatus))
        folder_statuses = {fs.folder_path: fs.status for fs in result.scalars().all()}

        # Get file counts and chunk totals per index_folder
        result = db.execute(select(IndexedFile))
        indexed_files = result.scalars().all()

        folder_stats: dict[str, dict] = {}
        for f in indexed_files:
            idx_folder = getattr(f, "index_folder", None) or f.folder_path
            if idx_folder not in folder_stats:
                folder_stats[idx_folder] = {"file_count": 0, "total_chunks": 0}
            folder_stats[idx_folder]["file_count"] += 1
            folder_stats[idx_folder]["total_chunks"] += f.chunk_count

        # Get folder metadata
        all_folder_paths = list(set(folder_statuses.keys()) | set(folder_stats.keys()))
        result = db.execute(
            select(FileMetadata).where(FileMetadata.path.in_(all_folder_paths))
        )
        folder_metadata = {meta.path: meta.metadata_text for meta in result.scalars().all()}

        def is_folder_active(folder_path: str) -> bool:
            if user_active_folders is None:
                return True
            folder_normalized = folder_path.rstrip("/")
            for active_folder in user_active_folders:
                active_normalized = active_folder.rstrip("/")
                if folder_normalized == active_normalized or folder_normalized.startswith(active_normalized + "/"):
                    return True
            return False

        results = []
        for folder_path in all_folder_paths:
            if not is_folder_active(folder_path):
                continue

            stats = folder_stats.get(folder_path, {"file_count": 0, "total_chunks": 0})
            results.append(
                IndexedFolderInfo(
                    folder_path=folder_path,
                    status=folder_statuses.get(folder_path, "none"),
                    file_count=stats["file_count"],
                    total_chunks=stats["total_chunks"],
                    metadata=folder_metadata.get(folder_path),
                )
            )

        return results


@mcp.tool()
def get_file(file_path: str) -> FileContent:
    """Get the full content of an indexed file.

    Args:
        file_path: Path to the file (relative to root)

    Returns:
        File content with metadata

    Raises:
        ValueError: If the file is not indexed or cannot be read
    """
    settings = get_settings()
    engine = get_sync_engine()

    with Session(engine) as db:
        # Check if file is indexed
        result = db.execute(
            select(IndexedFile).where(IndexedFile.file_path == file_path)
        )
        indexed_file = result.scalar_one_or_none()

        if not indexed_file:
            raise ValueError(f"File is not indexed: {file_path}")

        # Get file metadata
        result = db.execute(
            select(FileMetadata).where(FileMetadata.path == file_path)
        )
        file_meta = result.scalar_one_or_none()
        metadata_text = file_meta.metadata_text if file_meta else None

    # Read and parse file content
    abs_path = settings.root_path / file_path

    if not abs_path.exists():
        raise ValueError(f"File not found on disk: {file_path}")

    parse_result = parse_file(abs_path)

    if not parse_result.success:
        raise ValueError(f"Failed to parse file: {parse_result.error}")

    return FileContent(
        file_path=file_path,
        file_name=abs_path.name,
        content=parse_result.content,
        chunk_count=indexed_file.chunk_count,
        metadata=metadata_text,
    )


@mcp.tool()
def get_chunk_range(
    file_path: str,
    first_chunk: int,
    last_chunk: int,
) -> ChunkRangeResult:
    """Get a range of chunks from an indexed file, merged with overlaps removed.

    Args:
        file_path: Path to the file (relative to root)
        first_chunk: First chunk index (0-based, inclusive)
        last_chunk: Last chunk index (inclusive)

    Returns:
        Merged text content with status metadata
    """
    MAX_CHUNKS = 20
    settings = get_settings()
    vector_store = get_vector_store()
    chunk_overlap = settings.chunk_overlap

    # Validate input
    if first_chunk < 0:
        return ChunkRangeResult(
            success=False,
            file_path=file_path,
            merged_text="",
            first_chunk=first_chunk,
            last_chunk=last_chunk,
            actual_first_chunk=0,
            actual_last_chunk=0,
            total_chunks_in_file=0,
            chunks_returned=0,
            truncated_to_limit=False,
            truncated_beyond_file=False,
            error="first_chunk must be >= 0",
        )

    if last_chunk < first_chunk:
        return ChunkRangeResult(
            success=False,
            file_path=file_path,
            merged_text="",
            first_chunk=first_chunk,
            last_chunk=last_chunk,
            actual_first_chunk=0,
            actual_last_chunk=0,
            total_chunks_in_file=0,
            chunks_returned=0,
            truncated_to_limit=False,
            truncated_beyond_file=False,
            error="last_chunk must be >= first_chunk",
        )

    # Apply chunk limit
    truncated_to_limit = False
    effective_last_chunk = last_chunk
    if (last_chunk - first_chunk + 1) > MAX_CHUNKS:
        effective_last_chunk = first_chunk + MAX_CHUNKS - 1
        truncated_to_limit = True

    # Get chunks from vector store
    chunks = vector_store.get_chunks_by_range(file_path, first_chunk, effective_last_chunk)

    if not chunks:
        # Check if file exists at all
        total_chunks = vector_store.count_by_file(file_path)
        if total_chunks == 0:
            return ChunkRangeResult(
                success=False,
                file_path=file_path,
                merged_text="",
                first_chunk=first_chunk,
                last_chunk=last_chunk,
                actual_first_chunk=0,
                actual_last_chunk=0,
                total_chunks_in_file=0,
                chunks_returned=0,
                truncated_to_limit=truncated_to_limit,
                truncated_beyond_file=False,
                error=f"File not found or not indexed: {file_path}",
            )
        else:
            # File exists but requested range is beyond file size
            return ChunkRangeResult(
                success=False,
                file_path=file_path,
                merged_text="",
                first_chunk=first_chunk,
                last_chunk=last_chunk,
                actual_first_chunk=0,
                actual_last_chunk=0,
                total_chunks_in_file=total_chunks,
                chunks_returned=0,
                truncated_to_limit=truncated_to_limit,
                truncated_beyond_file=True,
                error=f"Requested chunk range {first_chunk}-{last_chunk} is beyond file size ({total_chunks} chunks, indices 0-{total_chunks - 1})",
            )

    # Get total chunks in file from first chunk's metadata
    total_chunks_in_file = chunks[0].metadata.total_chunks

    # Check if we got fewer chunks than requested (beyond file boundary)
    actual_first_chunk = chunks[0].metadata.chunk_index
    actual_last_chunk = chunks[-1].metadata.chunk_index
    truncated_beyond_file = actual_last_chunk < effective_last_chunk

    # Merge chunks with overlap removal
    merged_text = _merge_chunks_with_overlap(chunks, chunk_overlap)

    return ChunkRangeResult(
        success=True,
        file_path=file_path,
        merged_text=merged_text,
        first_chunk=first_chunk,
        last_chunk=last_chunk,
        actual_first_chunk=actual_first_chunk,
        actual_last_chunk=actual_last_chunk,
        total_chunks_in_file=total_chunks_in_file,
        chunks_returned=len(chunks),
        truncated_to_limit=truncated_to_limit,
        truncated_beyond_file=truncated_beyond_file,
        error=None,
    )


@mcp.tool()
def get_file_uri(file_path: str) -> FileUriResult:
    """Get a download URI for a file, suitable for use with wget/curl.

    Use this to get a direct download link for any file in the data directory.
    The URI can be used with terminal tools like wget or curl to download the file.

    Args:
        file_path: Path to the file (relative to root), typically from search results

    Returns:
        URI and file metadata (size, mime_type)

    Raises:
        ValueError: If the file does not exist or path is invalid
    """
    settings = get_settings()
    root = settings.root_path

    # Resolve path securely
    if not file_path or file_path == "/":
        raise ValueError("File path required")

    clean_path = file_path.lstrip("/")
    full_path = (root / clean_path).resolve()

    # Security: ensure path is within root
    if not str(full_path).startswith(str(root)):
        raise ValueError("Invalid file path")

    if not full_path.exists():
        raise ValueError(f"File not found: {file_path}")

    if full_path.is_dir():
        raise ValueError("Cannot get URI for a directory")

    # Get file metadata
    stat = full_path.stat()
    size = stat.st_size

    # Determine MIME type
    mime_type, _ = mimetypes.guess_type(str(full_path))
    if mime_type is None:
        mime_type = "application/octet-stream"

    # Construct URI using main app host/port
    # Use the main app port (not MCP port) since the raw endpoint is on the main app
    host = settings.host
    port = settings.port

    # If host is 0.0.0.0, use localhost for the URI
    if host == "0.0.0.0":
        host = "localhost"

    uri = f"http://{host}:{port}/api/raw/{clean_path}"

    return FileUriResult(
        uri=uri,
        file_path=clean_path,
        file_name=full_path.name,
        size=size,
        mime_type=mime_type,
    )


class FolderActiveState(BaseModel):
    """Active state of a folder for a user."""

    folder_path: str = Field(description="Path to the folder")
    is_active: bool = Field(description="Whether the folder is active for search")


class SetFolderActiveResult(BaseModel):
    """Result of setting folder active state."""

    success: bool = Field(description="Whether the operation succeeded")
    folder_path: str = Field(description="Path to the folder")
    is_active: bool = Field(description="New active state")
    subfolders_updated: int = Field(description="Number of subfolders also updated")
    error: str | None = Field(description="Error message if success is False")


@mcp.tool()
def set_folder_active(
    folder_path: str,
    is_active: bool,
) -> SetFolderActiveResult:
    """Set a folder's active state for search. Also updates all subfolders to the same state.

    The visibility cascades recursively to all subdirectories in the filesystem.

    Args:
        folder_path: Path to the folder
        is_active: Whether to activate (True) or deactivate (False) the folder

    Returns:
        Result with number of subfolders updated. Error if X-User-Name header not provided.
    """
    user_name = _get_current_user_name()
    if not user_name:
        return SetFolderActiveResult(
            success=False,
            folder_path=folder_path,
            is_active=is_active,
            subfolders_updated=0,
            error="X-User-Name header required for this operation",
        )
    settings = get_settings()
    engine = get_sync_engine()

    # Resolve and validate the folder path
    root_path = settings.root_path
    if not folder_path or folder_path == "/":
        target_path = root_path
    else:
        clean_path = folder_path.lstrip("/")
        target_path = (root_path / clean_path).resolve()

    # Security check and existence check
    if not str(target_path).startswith(str(root_path)):
        return SetFolderActiveResult(
            success=False,
            folder_path=folder_path,
            is_active=is_active,
            subfolders_updated=0,
            error="Invalid folder path",
        )

    if not target_path.exists() or not target_path.is_dir():
        return SetFolderActiveResult(
            success=False,
            folder_path=folder_path,
            is_active=is_active,
            subfolders_updated=0,
            error=f"Folder not found: {folder_path}",
        )

    # Walk filesystem recursively to find all subdirectories
    folders_to_update = [folder_path]  # Include the target folder itself

    try:
        for item in target_path.rglob("*"):
            if item.is_dir() and not item.name.startswith("."):
                try:
                    relative_path = str(item.relative_to(root_path))
                    folders_to_update.append(relative_path)
                except ValueError:
                    continue
    except (PermissionError, OSError):
        pass  # Continue with folders we could access

    with Session(engine) as db:
        # Get or create user
        user = _get_or_create_user(db, user_name)

        # Update or create settings for all folders
        subfolders_updated = 0
        for f in folders_to_update:
            result = db.execute(
                select(UserFolderSetting).where(
                    UserFolderSetting.user_id == user.id,
                    UserFolderSetting.folder_path == f,
                )
            )
            setting = result.scalar_one_or_none()

            if setting:
                setting.search_active = is_active
            else:
                setting = UserFolderSetting(
                    user_id=user.id,
                    folder_path=f,
                    enabled=False,  # Don't auto-enable indexing
                    search_active=is_active,
                )
                db.add(setting)

            if f != folder_path:
                subfolders_updated += 1

        db.commit()

        return SetFolderActiveResult(
            success=True,
            folder_path=folder_path,
            is_active=is_active,
            subfolders_updated=subfolders_updated,
            error=None,
        )


@mcp.tool()
def get_folder_active_states() -> list[FolderActiveState]:
    """Get the active states of all indexed folders for the current user.

    Returns:
        List of folders with their active states

    """
    user_name = _get_current_user_name()
    engine = get_sync_engine()

    with Session(engine) as db:
        user_settings = {}
        if user_name:
            user = _get_or_create_user(db, user_name)
            result = db.execute(
                select(UserFolderSetting).where(UserFolderSetting.user_id == user.id)
            )
            user_settings = {s.folder_path: s.search_active for s in result.scalars().all()}

        # Get all indexed folders
        result = db.execute(select(FolderIndexStatus.folder_path))
        all_folders = [row[0] for row in result.fetchall()]

        # Without a user, all folders are active
        results = []
        for folder in all_folders:
            is_active = user_settings.get(folder, False) if user_name else True
            results.append(
                FolderActiveState(
                    folder_path=folder,
                    is_active=is_active,
                )
            )

        return results


def _merge_chunks_with_overlap(chunks: list, chunk_overlap: int) -> str:
    """Merge consecutive chunks by removing overlapping text.

    Args:
        chunks: List of StoredChunk objects sorted by chunk_index
        chunk_overlap: Number of characters that overlap between chunks

    Returns:
        Merged text with overlaps removed
    """
    if not chunks:
        return ""

    if len(chunks) == 1:
        return chunks[0].text

    # Start with first chunk's full text
    merged = chunks[0].text

    # For each subsequent chunk, remove the overlap from its beginning
    for i in range(1, len(chunks)):
        chunk_text = chunks[i].text

        if chunk_overlap > 0 and len(chunk_text) > chunk_overlap:
            # Remove overlap from the beginning of this chunk
            merged += chunk_text[chunk_overlap:]
        else:
            # No overlap or chunk is too small
            merged += chunk_text

    return merged


def run_server():
    """Run the MCP server."""
    settings = get_settings()
    port = settings.mcp_port
    host = settings.host
    transport = settings.mcp_transport

    logger.info(f"Starting MCP server on {host}:{port} (transport: {transport})")

    # Add middleware to extract user header
    # Access the underlying Starlette app and add middleware
    app = mcp.http_app(transport=transport)
    app.add_middleware(UserHeaderMiddleware)

    # Run with configured transport
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_server()
