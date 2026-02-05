"""Raw file download API route (unauthenticated)."""

import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from ...config import get_settings

router = APIRouter()


@router.get("/{path:path}")
async def get_raw_file(path: str):
    """Download a raw file without authentication.

    This endpoint is designed for use with CLI tools like wget/curl.
    """
    settings = get_settings()
    root = settings.root_path

    # Resolve path securely
    if not path or path == "/":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File path required",
        )

    clean_path = path.lstrip("/")
    full_path = (root / clean_path).resolve()

    # Security: ensure path is within root
    if not str(full_path).startswith(str(root)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    if not full_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File not found: {path}",
        )

    if full_path.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot download a directory",
        )

    # Determine MIME type
    mime_type, _ = mimetypes.guess_type(str(full_path))
    if mime_type is None:
        mime_type = "application/octet-stream"

    return FileResponse(
        path=full_path,
        filename=full_path.name,
        media_type=mime_type,
    )
