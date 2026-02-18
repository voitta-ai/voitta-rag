"""File operations API routes."""

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel

from ..deps import CurrentUser, Filesystem

router = APIRouter()


class FileInfoResponse(BaseModel):
    """Response model for file info."""

    name: str
    path: str
    is_dir: bool
    size: int
    modified_at: str
    extension: str | None = None


@router.post("/upload")
async def upload_file(
    user: CurrentUser,
    fs: Filesystem,
    file: UploadFile = File(...),
    path: str = Form(""),
):
    """Upload a file to the specified path."""
    if path == "Anamnesis" or path.startswith("Anamnesis/"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Anamnesis folder is read-only")
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No filename provided",
        )

    try:
        info = fs.upload_file(path, file.filename, file.file)
        return FileInfoResponse(
            name=info.name,
            path=info.path,
            is_dir=info.is_dir,
            size=info.size,
            modified_at=info.modified_at.isoformat(),
            extension=info.extension,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except NotADirectoryError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/{path:path}")
async def get_file_info(
    path: str,
    user: CurrentUser,
    fs: Filesystem,
):
    """Get information about a file."""
    try:
        info = fs.get_info(path)
        return FileInfoResponse(
            name=info.name,
            path=info.path,
            is_dir=info.is_dir,
            size=info.size,
            modified_at=info.modified_at.isoformat(),
            extension=info.extension,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
