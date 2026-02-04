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
    search_active: bool = False


class FolderSettingsListResponse(BaseModel):
    """Response model for list of folder settings."""

    settings: list[FolderSettingResponse]


class ToggleFolderRequest(BaseModel):
    """Request model for toggling folder."""

    enabled: bool


class ToggleSearchActiveRequest(BaseModel):
    """Request model for toggling search active state."""

    search_active: bool


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
            FolderSettingResponse(
                folder_path=s.folder_path,
                enabled=s.enabled,
                search_active=s.search_active,
            )
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
        search_active=setting.search_active if setting else False,
    )


class ReindexResponse(BaseModel):
    """Response model for reindex request."""

    folder_path: str
    status: str
    message: str


@router.post("/folders/{path:path}/reindex")
async def reindex_folder(
    path: str,
    user: CurrentUser,
    fs: Filesystem,
    db: DB,
):
    """Force re-index a folder by setting status to pending."""
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

    # Check if folder is enabled for this user
    result = await db.execute(
        select(UserFolderSetting).where(
            UserFolderSetting.user_id == user.id,
            UserFolderSetting.folder_path == path,
        )
    )
    setting = result.scalar_one_or_none()

    if not setting or not setting.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Folder must be enabled for indexing before re-indexing",
        )

    # Find or create index status and set to pending
    result = await db.execute(
        select(FolderIndexStatus).where(FolderIndexStatus.folder_path == path)
    )
    index_status = result.scalar_one_or_none()

    if index_status:
        if index_status.status == "indexing":
            return ReindexResponse(
                folder_path=path,
                status=index_status.status,
                message="Folder is already being indexed",
            )
        index_status.status = "pending"
        index_status.error_message = None
    else:
        index_status = FolderIndexStatus(
            folder_path=path,
            status="pending",
        )
        db.add(index_status)

    await db.flush()

    return ReindexResponse(
        folder_path=path,
        status="pending",
        message="Folder queued for re-indexing",
    )


@router.put("/folders/{path:path}/search-active")
async def toggle_search_active(
    path: str,
    request: ToggleSearchActiveRequest,
    user: CurrentUser,
    fs: Filesystem,
    db: DB,
):
    """Toggle folder search active state for MCP search filtering.

    When a folder is set to search_active=True, it (and optionally its subfolders)
    will be included in MCP search results for this user.
    """
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

    # Get all indexed folders to find this folder and subfolders
    result = await db.execute(select(FolderIndexStatus.folder_path))
    all_indexed_folders = [row[0] for row in result.fetchall()]

    # Find target folder and subfolders
    path_normalized = path.rstrip("/")
    folders_to_update = []

    for f in all_indexed_folders:
        f_normalized = f.rstrip("/")
        if f_normalized == path_normalized or f_normalized.startswith(path_normalized + "/"):
            folders_to_update.append(f)

    if not folders_to_update:
        # Folder not indexed yet - just update this folder
        folders_to_update = [path]

    # Update or create settings for all folders
    for folder in folders_to_update:
        result = await db.execute(
            select(UserFolderSetting).where(
                UserFolderSetting.user_id == user.id,
                UserFolderSetting.folder_path == folder,
            )
        )
        setting = result.scalar_one_or_none()

        if setting:
            setting.search_active = request.search_active
        else:
            setting = UserFolderSetting(
                user_id=user.id,
                folder_path=folder,
                enabled=False,  # Don't auto-enable indexing
                search_active=request.search_active,
            )
            db.add(setting)

    await db.flush()

    return FolderSettingResponse(
        folder_path=path,
        enabled=False,  # We don't change enabled
        search_active=request.search_active,
    )
