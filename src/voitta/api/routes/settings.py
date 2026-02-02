"""User settings API routes."""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from ..deps import DB, CurrentUser, Filesystem
from ...db.models import FolderIndexStatus, UserFolderSetting

router = APIRouter()


class FolderSettingResponse(BaseModel):
    """Response model for folder setting."""

    folder_path: str
    enabled: bool


class FolderSettingsListResponse(BaseModel):
    """Response model for list of folder settings."""

    settings: list[FolderSettingResponse]


class ToggleFolderRequest(BaseModel):
    """Request model for toggling folder."""

    enabled: bool


@router.get("/folders")
async def get_folder_settings(
    user: CurrentUser,
    db: DB,
):
    """Get all folder settings for current user."""
    result = await db.execute(
        select(UserFolderSetting).where(UserFolderSetting.user_id == user.id)
    )
    settings = result.scalars().all()

    return FolderSettingsListResponse(
        settings=[
            FolderSettingResponse(folder_path=s.folder_path, enabled=s.enabled)
            for s in settings
        ]
    )


@router.put("/folders/{path:path}")
async def toggle_folder(
    path: str,
    request: ToggleFolderRequest,
    user: CurrentUser,
    fs: Filesystem,
    db: DB,
):
    """Toggle folder enabled/disabled for downstream applications."""
    if not fs.exists(path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Folder not found: {path}",
        )

    if not fs.is_dir(path):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Not a folder: {path}",
        )

    # Find or create setting
    result = await db.execute(
        select(UserFolderSetting).where(
            UserFolderSetting.user_id == user.id,
            UserFolderSetting.folder_path == path,
        )
    )
    setting = result.scalar_one_or_none()

    if setting:
        setting.enabled = request.enabled
    else:
        setting = UserFolderSetting(
            user_id=user.id,
            folder_path=path,
            enabled=request.enabled,
        )
        db.add(setting)

    # Update folder index status when enabled
    if request.enabled:
        # Find or create index status
        result = await db.execute(
            select(FolderIndexStatus).where(FolderIndexStatus.folder_path == path)
        )
        index_status = result.scalar_one_or_none()

        if index_status:
            # Only set to pending if not already indexed or indexing
            if index_status.status not in ("indexed", "indexing"):
                index_status.status = "pending"
        else:
            index_status = FolderIndexStatus(
                folder_path=path,
                status="pending",
            )
            db.add(index_status)

    await db.flush()

    return FolderSettingResponse(
        folder_path=path,
        enabled=setting.enabled,
    )


@router.get("/folders/{path:path}")
async def get_folder_setting(
    path: str,
    user: CurrentUser,
    db: DB,
):
    """Get folder setting for a specific path."""
    result = await db.execute(
        select(UserFolderSetting).where(
            UserFolderSetting.user_id == user.id,
            UserFolderSetting.folder_path == path,
        )
    )
    setting = result.scalar_one_or_none()

    return FolderSettingResponse(
        folder_path=path,
        enabled=setting.enabled if setting else False,
    )
