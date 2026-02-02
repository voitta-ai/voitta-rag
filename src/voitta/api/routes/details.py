"""Item details API for sidebar."""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from ..deps import DB, CurrentUser, Filesystem, Metadata
from ...db.models import FolderIndexStatus, UserFolderSetting

router = APIRouter()


class ItemDetailsResponse(BaseModel):
    """Response with all details for sidebar display."""

    path: str
    name: str
    is_dir: bool
    # Metadata
    metadata_text: str | None = None
    metadata_updated_by: str | None = None
    # Folder-specific (only if is_dir)
    folder_enabled: bool | None = None
    index_status: str | None = None


@router.get("/{path:path}")
async def get_item_details(
    path: str,
    user: CurrentUser,
    fs: Filesystem,
    metadata_svc: Metadata,
    db: DB,
):
    """Get all details for an item (file or folder)."""
    if not fs.exists(path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Path not found: {path}",
        )

    info = fs.get_info(path)
    is_dir = info.is_dir

    # Get metadata
    metadata_text, metadata_updated_by = await metadata_svc.get_metadata_with_user(path)

    # Get folder-specific data
    folder_enabled = None
    index_status = None

    if is_dir:
        # Get folder enabled setting for this user
        result = await db.execute(
            select(UserFolderSetting).where(
                UserFolderSetting.user_id == user.id,
                UserFolderSetting.folder_path == path,
            )
        )
        setting = result.scalar_one_or_none()
        folder_enabled = setting.enabled if setting else False

        # Get index status
        result = await db.execute(
            select(FolderIndexStatus).where(FolderIndexStatus.folder_path == path)
        )
        status_row = result.scalar_one_or_none()
        index_status = status_row.status if status_row else "none"

    return ItemDetailsResponse(
        path=path,
        name=info.name,
        is_dir=is_dir,
        metadata_text=metadata_text,
        metadata_updated_by=metadata_updated_by,
        folder_enabled=folder_enabled,
        index_status=index_status,
    )
