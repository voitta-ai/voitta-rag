"""Folder operations API routes."""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from ..deps import CurrentUser, Filesystem

router = APIRouter()


class CreateFolderRequest(BaseModel):
    """Request model for creating a folder."""

    name: str
    path: str = ""


class FolderItemResponse(BaseModel):
    """Response model for folder item."""

    name: str
    path: str
    is_dir: bool
    size: int
    modified_at: str
    extension: str | None = None


class FolderListResponse(BaseModel):
    """Response model for folder listing."""

    items: list[FolderItemResponse]
    path: str


@router.post("")
async def create_folder(
    request: CreateFolderRequest,
    user: CurrentUser,
    fs: Filesystem,
):
    """Create a new folder."""
    try:
        info = fs.create_folder(request.path, request.name)
        return FolderItemResponse(
            name=info.name,
            path=info.path,
            is_dir=info.is_dir,
            size=info.size,
            modified_at=info.modified_at.isoformat(),
            extension=info.extension,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except FileExistsError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/{path:path}")
async def list_folder(
    path: str,
    user: CurrentUser,
    fs: Filesystem,
):
    """List contents of a folder."""
    try:
        items = fs.list_directory(path)
        return FolderListResponse(
            items=[
                FolderItemResponse(
                    name=item.name,
                    path=item.path,
                    is_dir=item.is_dir,
                    size=item.size,
                    modified_at=item.modified_at.isoformat(),
                    extension=item.extension,
                )
                for item in items
            ],
            path=path,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except NotADirectoryError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("")
async def list_root_folder(
    user: CurrentUser,
    fs: Filesystem,
):
    """List contents of root folder."""
    items = fs.list_directory("")
    return FolderListResponse(
        items=[
            FolderItemResponse(
                name=item.name,
                path=item.path,
                is_dir=item.is_dir,
                size=item.size,
                modified_at=item.modified_at.isoformat(),
                extension=item.extension,
            )
            for item in items
        ],
        path="",
    )
