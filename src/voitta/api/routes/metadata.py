"""Metadata API routes."""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from ..deps import CurrentUser, Filesystem, Metadata

router = APIRouter()


class MetadataResponse(BaseModel):
    """Response model for metadata."""

    path: str
    metadata_text: str | None
    updated_by: str | None = None


class UpdateMetadataRequest(BaseModel):
    """Request model for updating metadata."""

    text: str


@router.get("/{path:path}")
async def get_metadata(
    path: str,
    user: CurrentUser,
    fs: Filesystem,
    metadata_svc: Metadata,
):
    """Get metadata for a file or folder."""
    if not fs.exists(path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Path not found: {path}",
        )

    text, updated_by = await metadata_svc.get_metadata_with_user(path)
    return MetadataResponse(
        path=path,
        metadata_text=text,
        updated_by=updated_by,
    )


@router.put("/{path:path}")
async def update_metadata(
    path: str,
    request: UpdateMetadataRequest,
    user: CurrentUser,
    fs: Filesystem,
    metadata_svc: Metadata,
):
    """Update metadata for a file or folder."""
    if not fs.exists(path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Path not found: {path}",
        )

    await metadata_svc.set_metadata(path, request.text, user.id)
    return MetadataResponse(
        path=path,
        metadata_text=request.text,
        updated_by=user.name,
    )


@router.delete("/{path:path}")
async def delete_metadata(
    path: str,
    user: CurrentUser,
    metadata_svc: Metadata,
):
    """Delete metadata for a file or folder."""
    deleted = await metadata_svc.delete_metadata(path)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No metadata found for: {path}",
        )
    return {"status": "deleted", "path": path}
