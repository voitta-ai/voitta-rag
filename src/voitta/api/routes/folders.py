"""Folder operations API routes."""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from ..deps import CurrentUser, DB, Filesystem
from ...db.models import FolderIndexStatus, FolderSyncSource, IndexedFile, UserFolderSetting

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


@router.delete("/{path:path}")
async def delete_folder(
    path: str,
    user: CurrentUser,
    fs: Filesystem,
    db: DB,
):
    """Delete a folder and all its contents, including associated DB records."""
    from ...services.watcher import file_watcher

    # Suppress watcher events during bulk delete
    file_watcher.suppress_path(path)
    try:
        # Clean up associated DB records
        # 1. Remove sync source
        result = await db.execute(
            select(FolderSyncSource).where(FolderSyncSource.folder_path == path)
        )
        source = result.scalar_one_or_none()
        if source:
            await db.delete(source)

        # 2. Remove index status
        result = await db.execute(
            select(FolderIndexStatus).where(FolderIndexStatus.folder_path == path)
        )
        idx_status = result.scalar_one_or_none()
        if idx_status:
            await db.delete(idx_status)

        # 3. Remove indexed file records
        result = await db.execute(
            select(IndexedFile).where(IndexedFile.index_folder == path)
        )
        for indexed_file in result.scalars().all():
            await db.delete(indexed_file)

        # 4. Remove user folder settings
        result = await db.execute(
            select(UserFolderSetting).where(UserFolderSetting.folder_path == path)
        )
        for setting in result.scalars().all():
            await db.delete(setting)

        # 5. Remove from vector store
        try:
            from ...services.vector_store import VectorStoreService
            vector_store = VectorStoreService()
            vector_store.delete_by_index_folder(path)
        except Exception:
            pass  # Vector store may not be available

        # 6. Delete the folder from disk
        fs.delete_folder(path)

        await db.flush()
        return {"ok": True}
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except NotADirectoryError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    finally:
        file_watcher.unsuppress_path(path)


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
