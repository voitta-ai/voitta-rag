"""Remote sync API routes."""

import base64
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select

from ..deps import DB, CurrentUser, Filesystem
from ...config import get_settings
from ...db.database import get_db_context
from ...db.models import FolderSyncSource, utc_now
from ...services.sync import get_connector

logger = logging.getLogger(__name__)

router = APIRouter()


# --- Pydantic schemas ---


class SharePointConfig(BaseModel):
    tenant_id: str
    client_id: str
    client_secret: str
    site_url: str
    drive_id: str = ""
    connected: bool = False


class GoogleDriveConfig(BaseModel):
    service_account_json: str
    folder_id: str


class GitHubConfig(BaseModel):
    token: str
    repo: str
    branch: str = "main"
    path: str = ""


class UpsertSyncSourceRequest(BaseModel):
    source_type: str
    sharepoint: SharePointConfig | None = None
    google_drive: GoogleDriveConfig | None = None
    github: GitHubConfig | None = None


class SyncSourceResponse(BaseModel):
    folder_path: str
    source_type: str
    sync_status: str
    sync_error: str | None = None
    last_synced_at: str | None = None
    sharepoint: SharePointConfig | None = None
    google_drive: GoogleDriveConfig | None = None
    github: GitHubConfig | None = None


class SyncStatusResponse(BaseModel):
    folder_path: str
    sync_status: str
    sync_error: str | None = None
    last_synced_at: str | None = None


class SyncTriggerResponse(BaseModel):
    folder_path: str
    status: str
    message: str


# --- Helpers ---


def _to_response(source: FolderSyncSource) -> SyncSourceResponse:
    sp = None
    gd = None
    gh = None

    if source.source_type == "sharepoint":
        sp = SharePointConfig(
            tenant_id=source.sp_tenant_id or "",
            client_id=source.sp_client_id or "",
            client_secret=source.sp_client_secret or "",
            site_url=source.sp_site_url or "",
            drive_id=source.sp_drive_id or "",
            connected=bool(source.sp_refresh_token),
        )
    elif source.source_type == "google_drive":
        gd = GoogleDriveConfig(
            service_account_json=source.gd_service_account_json or "",
            folder_id=source.gd_folder_id or "",
        )
    elif source.source_type == "github":
        gh = GitHubConfig(
            token=source.gh_token or "",
            repo=source.gh_repo or "",
            branch=source.gh_branch or "main",
            path=source.gh_path or "",
        )

    return SyncSourceResponse(
        folder_path=source.folder_path,
        source_type=source.source_type,
        sync_status=source.sync_status or "idle",
        sync_error=source.sync_error,
        last_synced_at=source.last_synced_at.isoformat() if source.last_synced_at else None,
        sharepoint=sp,
        google_drive=gd,
        github=gh,
    )


def _is_folder_empty(fs: Filesystem, path: str) -> bool:
    """Check if a folder has no files (recursive)."""
    return fs.count_files_recursive(path) == 0


def _get_redirect_uri() -> str:
    """Get the SharePoint OAuth redirect URI."""
    settings = get_settings()
    return f"{settings.base_url}/api/sync/sharepoint/callback"


# --- SharePoint OAuth endpoints ---
# NOTE: These must be registered BEFORE the catch-all {path:path} routes


@router.get("/sharepoint/callback")
async def sharepoint_oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
):
    """Handle the OAuth2 redirect from Microsoft."""
    from ...services.sync.sharepoint import exchange_code_for_tokens

    # Decode folder path from state
    try:
        folder_path = base64.urlsafe_b64decode(state.encode()).decode()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid state parameter")

    # Look up the sync source to get credentials
    async with get_db_context() as db:
        result = await db.execute(
            select(FolderSyncSource).where(FolderSyncSource.folder_path == folder_path)
        )
        source = result.scalar_one_or_none()
        if not source or source.source_type != "sharepoint":
            raise HTTPException(status_code=404, detail="SharePoint sync source not found")

        # Exchange code for tokens
        tokens = await exchange_code_for_tokens(
            tenant_id=source.sp_tenant_id,
            client_id=source.sp_client_id,
            client_secret=source.sp_client_secret,
            code=code,
            redirect_uri=_get_redirect_uri(),
        )

        source.sp_refresh_token = tokens["refresh_token"]

    # Redirect back to the folder's browse page
    browse_path = f"/browse/{folder_path}" if folder_path else "/browse"
    return RedirectResponse(url=browse_path, status_code=302)


@router.get("/sharepoint/auth")
async def sharepoint_auth_initiate(
    folder_path: str = Query(...),
    user: CurrentUser = None,
    db: DB = None,
):
    """Generate the Microsoft OAuth2 authorization URL."""
    from ...services.sync.sharepoint import get_auth_url

    # Verify sync source exists
    result = await db.execute(
        select(FolderSyncSource).where(FolderSyncSource.folder_path == folder_path)
    )
    source = result.scalar_one_or_none()
    if not source or source.source_type != "sharepoint":
        raise HTTPException(status_code=404, detail="SharePoint sync source not found")

    if not source.sp_tenant_id or not source.sp_client_id:
        raise HTTPException(
            status_code=400,
            detail="Save SharePoint configuration (tenant ID, client ID, etc.) before connecting",
        )

    state = base64.urlsafe_b64encode(source.folder_path.encode()).decode()

    auth_url = get_auth_url(
        tenant_id=source.sp_tenant_id,
        client_id=source.sp_client_id,
        redirect_uri=_get_redirect_uri(),
        state=state,
    )

    return {"auth_url": auth_url}


# --- CRUD + sync endpoints ---


@router.get("/{path:path}/status", response_model=SyncStatusResponse)
async def get_sync_status(path: str, user: CurrentUser, db: DB):
    """Poll sync status for a folder."""
    result = await db.execute(
        select(FolderSyncSource).where(FolderSyncSource.folder_path == path)
    )
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No sync source configured",
        )

    return SyncStatusResponse(
        folder_path=source.folder_path,
        sync_status=source.sync_status or "idle",
        sync_error=source.sync_error,
        last_synced_at=source.last_synced_at.isoformat() if source.last_synced_at else None,
    )


@router.post("/{path:path}/trigger", response_model=SyncTriggerResponse)
async def trigger_sync(
    path: str,
    user: CurrentUser,
    fs: Filesystem,
    db: DB,
    background_tasks: BackgroundTasks,
):
    """Trigger a sync for a folder."""
    result = await db.execute(
        select(FolderSyncSource).where(FolderSyncSource.folder_path == path)
    )
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No sync source configured for this folder",
        )

    if source.sync_status == "syncing":
        return SyncTriggerResponse(
            folder_path=path, status="syncing", message="Sync already in progress"
        )

    source.sync_status = "syncing"
    source.sync_error = None
    await db.flush()

    background_tasks.add_task(_run_sync, path)

    return SyncTriggerResponse(
        folder_path=path, status="syncing", message="Sync started"
    )


@router.get("/{path:path}", response_model=SyncSourceResponse | None)
async def get_sync_source(path: str, user: CurrentUser, db: DB):
    """Get sync configuration for a folder."""
    result = await db.execute(
        select(FolderSyncSource).where(FolderSyncSource.folder_path == path)
    )
    source = result.scalar_one_or_none()
    if not source:
        return None
    return _to_response(source)


@router.put("/{path:path}", response_model=SyncSourceResponse)
async def upsert_sync_source(
    path: str,
    request: UpsertSyncSourceRequest,
    user: CurrentUser,
    fs: Filesystem,
    db: DB,
):
    """Create or update sync source for a folder."""
    if not fs.exists(path) or not fs.is_dir(path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Folder not found",
        )

    # Check: folder must be empty or already have a sync source
    result = await db.execute(
        select(FolderSyncSource).where(FolderSyncSource.folder_path == path)
    )
    existing = result.scalar_one_or_none()

    if not existing and not _is_folder_empty(fs, path):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Sync can only be configured on empty folders",
        )

    if request.source_type not in ("sharepoint", "google_drive", "github"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown source type: {request.source_type}",
        )

    if existing:
        source = existing
    else:
        source = FolderSyncSource(folder_path=path)
        db.add(source)

    source.source_type = request.source_type

    # Clear all credential fields first
    for field in (
        "sp_tenant_id", "sp_client_id", "sp_client_secret", "sp_site_url", "sp_drive_id",
        "sp_refresh_token",
        "gd_service_account_json", "gd_folder_id",
        "gh_token", "gh_repo", "gh_branch", "gh_path",
    ):
        setattr(source, field, None)

    # Set connector-specific fields
    if request.source_type == "sharepoint" and request.sharepoint:
        source.sp_tenant_id = request.sharepoint.tenant_id
        source.sp_client_id = request.sharepoint.client_id
        source.sp_client_secret = request.sharepoint.client_secret
        source.sp_site_url = request.sharepoint.site_url
        source.sp_drive_id = request.sharepoint.drive_id
    elif request.source_type == "google_drive" and request.google_drive:
        source.gd_service_account_json = request.google_drive.service_account_json
        source.gd_folder_id = request.google_drive.folder_id
    elif request.source_type == "github" and request.github:
        source.gh_token = request.github.token
        source.gh_repo = request.github.repo
        source.gh_branch = request.github.branch
        source.gh_path = request.github.path

    await db.flush()
    return _to_response(source)


@router.delete("/{path:path}")
async def delete_sync_source(path: str, user: CurrentUser, db: DB):
    """Remove sync configuration for a folder."""
    result = await db.execute(
        select(FolderSyncSource).where(FolderSyncSource.folder_path == path)
    )
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No sync source configured for this folder",
        )

    await db.delete(source)
    await db.flush()
    return {"ok": True}


# --- Background task ---


async def _run_sync(folder_path: str):
    """Run sync in background."""
    from ...services.filesystem import FilesystemService

    async with get_db_context() as db:
        result = await db.execute(
            select(FolderSyncSource).where(FolderSyncSource.folder_path == folder_path)
        )
        source = result.scalar_one_or_none()
        if not source:
            return

        try:
            connector = get_connector(source.source_type)
            fs = FilesystemService()
            await connector.sync(source, fs)
            source.sync_status = "synced"
            source.sync_error = None
            source.last_synced_at = utc_now()
        except Exception as e:
            logger.exception("Sync failed for %s", folder_path)
            source.sync_status = "error"
            source.sync_error = str(e)
