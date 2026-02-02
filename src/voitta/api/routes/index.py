"""Index job API routes (placeholder)."""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from ..deps import CurrentUser, Filesystem

router = APIRouter()


class IndexJobResponse(BaseModel):
    """Response model for index job."""

    path: str
    status: str
    message: str


@router.post("/{path:path}")
async def trigger_index(
    path: str,
    user: CurrentUser,
    fs: Filesystem,
):
    """Trigger indexing for a folder (placeholder)."""
    if not fs.exists(path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Path not found: {path}",
        )

    if not fs.is_dir(path):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Not a folder: {path}",
        )

    # Placeholder - actual indexing will be implemented later
    return IndexJobResponse(
        path=path,
        status="queued",
        message=f"Index job for '{path}' has been queued (placeholder - not yet implemented)",
    )


@router.post("/reindex/{path:path}")
async def trigger_reindex(
    path: str,
    user: CurrentUser,
    fs: Filesystem,
):
    """Trigger re-indexing for a folder (placeholder)."""
    if not fs.exists(path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Path not found: {path}",
        )

    if not fs.is_dir(path):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Not a folder: {path}",
        )

    # Placeholder - actual indexing will be implemented later
    return IndexJobResponse(
        path=path,
        status="queued",
        message=f"Re-index job for '{path}' has been queued (placeholder - not yet implemented)",
    )
