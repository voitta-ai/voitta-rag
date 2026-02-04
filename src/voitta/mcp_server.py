"""MCP server for voitta-rag RAG capabilities."""

import logging
from contextvars import ContextVar
from pathlib import Path

from fastmcp import FastMCP
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from .config import get_settings
from .db.database import get_sync_engine
from .db.models import FileMetadata, FolderIndexStatus, IndexedFile, User, UserFolderSetting
from .services.embedding import get_embedding_service
from .services.parsers import parse_file
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
            print("🔑 MCP request (no user header)", flush=True)
            current_user.set(None)
        response = await call_next(request)
        return response


class AuthError(Exception):
    """Raised when user is not authorized."""

    pass


def _get_current_user_name() -> str:
    """Get the current user name from context, or raise AuthError."""
    user_name = current_user.get()
    if not user_name:
        raise AuthError("Not authorized: X-User-Name header required")
    return user_name


def _get_or_create_user(db: Session, user_name: str) -> User:
    """Get existing user or create a new one."""
    result = db.execute(select(User).where(User.name == user_name))
    user = result.scalar_one_or_none()
    if not user:
        user = User(name=user_name)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def _get_user_active_folders(db: Session, user_id: int) -> list[str]:
    """Get list of folder paths that are active for the user."""
    result = db.execute(
        select(UserFolderSetting.folder_path).where(
            UserFolderSetting.user_id == user_id,
            UserFolderSetting.enabled == True,  # noqa: E712
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


@mcp.tool()
def search(
    query: str,
    limit: int | None = None,
    include_folders: list[str] | None = None,
    exclude_folders: list[str] | None = None,
) -> list[SearchResult]:
    """Search indexed documents using semantic similarity.

    Args:
        query: The search query text
        limit: Maximum number of results to return (default from MCP_SEARCH_LIMIT env var)
        include_folders: Optional list of folder paths to search within (searches all if not specified)
        exclude_folders: Optional list of folder paths to exclude from search

    Returns:
        List of matching document chunks with metadata and similarity scores

    Raises:
        AuthError: If X-User-Name header is not provided
    """
    # Require authentication
    user_name = _get_current_user_name()

    settings = get_settings()
    if limit is None:
        limit = settings.mcp_search_limit
    embedding_service = get_embedding_service()
    vector_store = get_vector_store()
    engine = get_sync_engine()

    with Session(engine) as db:
        # Get or create user
        user = _get_or_create_user(db, user_name)

        # Get user's active folders
        user_active_folders = _get_user_active_folders(db, user.id)

        # If no active folders, return empty results
        if not user_active_folders:
            return []

        # Get disabled folders to exclude from search (by index_folder)
        result = db.execute(
            select(FolderIndexStatus.folder_path).where(FolderIndexStatus.status == "disabled")
        )
        disabled_index_folders = [row[0] for row in result.fetchall()]

    # Generate query embedding
    query_embedding = embedding_service.embed_query(query)

    # Combine user's active folders with any explicit include_folders filter
    # If include_folders is specified, intersect with user's active folders
    effective_include_folders = user_active_folders
    if include_folders:
        # Only include folders that are both in user's active list AND in the requested list
        effective_include_folders = [f for f in include_folders if f in user_active_folders]
        if not effective_include_folders:
            return []  # No overlap between requested and active folders

    # Search vector store (excluding disabled index_folders)
    chunks = vector_store.search(
        query_embedding=query_embedding,
        limit=limit,
        include_folders=effective_include_folders,
        exclude_folders=exclude_folders,
        exclude_index_folders=disabled_index_folders if disabled_index_folders else None,
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
        List of indexed folders with status, file counts, and user metadata
    """
    engine = get_sync_engine()

    with Session(engine) as db:
        # Get all folder index statuses
        result = db.execute(select(FolderIndexStatus))
        folder_statuses = {fs.folder_path: fs.status for fs in result.scalars().all()}

        # Get file counts and chunk totals per index_folder (the folder at which indexing was triggered)
        result = db.execute(select(IndexedFile))
        indexed_files = result.scalars().all()

        folder_stats: dict[str, dict] = {}
        for f in indexed_files:
            # Use index_folder if available, otherwise fall back to folder_path
            idx_folder = getattr(f, "index_folder", None) or f.folder_path
            if idx_folder not in folder_stats:
                folder_stats[idx_folder] = {"file_count": 0, "total_chunks": 0}
            folder_stats[idx_folder]["file_count"] += 1
            folder_stats[idx_folder]["total_chunks"] += f.chunk_count

        # Get folder metadata
        folder_paths = list(set(folder_statuses.keys()) | set(folder_stats.keys()))
        result = db.execute(
            select(FileMetadata).where(FileMetadata.path.in_(folder_paths))
        )
        folder_metadata = {meta.path: meta.metadata_text for meta in result.scalars().all()}

        # Build results
        results = []
        for folder_path in folder_paths:
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

    Args:
        folder_path: Path to the folder
        is_active: Whether to activate (True) or deactivate (False) the folder

    Returns:
        Result with number of subfolders updated

    Raises:
        AuthError: If X-User-Name header is not provided
    """
    user_name = _get_current_user_name()
    engine = get_sync_engine()

    with Session(engine) as db:
        # Get or create user
        user = _get_or_create_user(db, user_name)

        # Get all indexed folders to find subfolders
        result = db.execute(select(FolderIndexStatus.folder_path))
        all_folders = [row[0] for row in result.fetchall()]

        # Find the target folder and all its subfolders
        folders_to_update = []
        folder_path_normalized = folder_path.rstrip("/")

        for f in all_folders:
            f_normalized = f.rstrip("/")
            # Match exact folder or subfolders (folders that start with folder_path/)
            if f_normalized == folder_path_normalized or f_normalized.startswith(folder_path_normalized + "/"):
                folders_to_update.append(f)

        if not folders_to_update:
            return SetFolderActiveResult(
                success=False,
                folder_path=folder_path,
                is_active=is_active,
                subfolders_updated=0,
                error=f"Folder not found: {folder_path}",
            )

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
                setting.enabled = is_active
            else:
                setting = UserFolderSetting(
                    user_id=user.id,
                    folder_path=f,
                    enabled=is_active,
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

    Raises:
        AuthError: If X-User-Name header is not provided
    """
    user_name = _get_current_user_name()
    engine = get_sync_engine()

    with Session(engine) as db:
        # Get or create user
        user = _get_or_create_user(db, user_name)

        # Get all indexed folders
        result = db.execute(select(FolderIndexStatus.folder_path))
        all_folders = [row[0] for row in result.fetchall()]

        # Get user's folder settings
        result = db.execute(
            select(UserFolderSetting).where(UserFolderSetting.user_id == user.id)
        )
        user_settings = {s.folder_path: s.enabled for s in result.scalars().all()}

        # Build results with default=False for folders without settings
        results = []
        for folder in all_folders:
            results.append(
                FolderActiveState(
                    folder_path=folder,
                    is_active=user_settings.get(folder, False),
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
