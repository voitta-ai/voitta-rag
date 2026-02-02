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
from .db.models import FileMetadata, FolderIndexStatus, IndexedFile
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


class SearchResult(BaseModel):
    """A single search result."""

    text: str = Field(description="The text content of the chunk")
    score: float = Field(description="Similarity score (0-1, higher is better)")
    file_path: str = Field(description="Path to the source file")
    file_name: str = Field(description="Name of the source file")
    folder_path: str = Field(description="Folder containing the file")
    chunk_index: int = Field(description="Index of this chunk within the file")
    total_chunks: int = Field(description="Total number of chunks in the file")
    file_metadata: str | None = Field(description="User-added metadata/notes for the file")


class IndexedFolderInfo(BaseModel):
    """Information about an indexed folder."""

    folder_path: str = Field(description="Path to the folder")
    status: str = Field(description="Index status: indexed, indexing, pending, error, none")
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
    """
    settings = get_settings()
    if limit is None:
        limit = settings.mcp_search_limit
    embedding_service = get_embedding_service()
    vector_store = get_vector_store()
    engine = get_sync_engine()

    # Generate query embedding
    query_embedding = embedding_service.embed_query(query)

    # Search vector store
    chunks = vector_store.search(
        query_embedding=query_embedding,
        limit=limit,
        include_folders=include_folders,
        exclude_folders=exclude_folders,
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

        # Get file counts and chunk totals per folder
        result = db.execute(select(IndexedFile))
        indexed_files = result.scalars().all()

        folder_stats: dict[str, dict] = {}
        for f in indexed_files:
            if f.folder_path not in folder_stats:
                folder_stats[f.folder_path] = {"file_count": 0, "total_chunks": 0}
            folder_stats[f.folder_path]["file_count"] += 1
            folder_stats[f.folder_path]["total_chunks"] += f.chunk_count

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
