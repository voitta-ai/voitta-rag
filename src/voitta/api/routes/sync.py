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


class AzureDevOpsConfig(BaseModel):
    tenant_id: str
    client_id: str
    client_secret: str
    url: str  # https://dev.azure.com/{org}/{project}
    organization: str = ""
    project: str = ""
    connected: bool = False


class UpsertSyncSourceRequest(BaseModel):
    source_type: str
    sharepoint: SharePointConfig | None = None
    google_drive: GoogleDriveConfig | None = None
    github: GitHubConfig | None = None
    azure_devops: AzureDevOpsConfig | None = None


class SyncSourceResponse(BaseModel):
    folder_path: str
    source_type: str
    sync_status: str
    sync_error: str | None = None
    last_synced_at: str | None = None
    sharepoint: SharePointConfig | None = None
    google_drive: GoogleDriveConfig | None = None
    github: GitHubConfig | None = None
    azure_devops: AzureDevOpsConfig | None = None


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
    ado = None

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
    elif source.source_type == "azure_devops":
        ado = AzureDevOpsConfig(
            tenant_id=source.ado_tenant_id or "",
            client_id=source.ado_client_id or "",
            client_secret=source.ado_client_secret or "",
            url=source.ado_url or "",
            organization=source.ado_organization or "",
            project=source.ado_project or "",
            connected=bool(source.ado_refresh_token),
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
        azure_devops=ado,
    )


def _is_folder_empty(fs: Filesystem, path: str) -> bool:
    """Check if a folder has no files (recursive)."""
    return fs.count_files_recursive(path) == 0


def _get_oauth_redirect_uri() -> str:
    """Get the unified OAuth redirect URI (shared by SharePoint and Azure DevOps)."""
    settings = get_settings()
    return f"{settings.base_url}/api/sync/oauth/callback"


# Keep legacy SharePoint redirect URI so existing refresh tokens still work
def _get_redirect_uri() -> str:
    return _get_oauth_redirect_uri()


# --- OAuth endpoints (unified for SharePoint + Azure DevOps) ---
# NOTE: These must be registered BEFORE the catch-all {path:path} routes

# OAuth config per source type: (tenant_id_field, client_id_field, client_secret_field,
#                                  refresh_token_field, exchange_fn_import, scopes_module,
#                                  auth_fn_import, ws_event_type)
_OAUTH_SOURCES = {
    "sharepoint": {
        "tenant_id": "sp_tenant_id",
        "client_id": "sp_client_id",
        "client_secret": "sp_client_secret",
        "refresh_token": "sp_refresh_token",
        "exchange_fn": "services.sync.sharepoint",
        "auth_fn": "services.sync.sharepoint",
        "ws_event": "sp_connected",
    },
    "azure_devops": {
        "tenant_id": "ado_tenant_id",
        "client_id": "ado_client_id",
        "client_secret": "ado_client_secret",
        "refresh_token": "ado_refresh_token",
        "exchange_fn": "services.sync.azure_devops",
        "auth_fn": "services.sync.azure_devops",
        "ws_event": "ado_connected",
    },
}


@router.get("/oauth/callback")
async def oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
):
    """Unified OAuth2 callback — dispatches by source_type."""
    try:
        folder_path = base64.urlsafe_b64decode(state.encode()).decode()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid state parameter")

    async with get_db_context() as db:
        result = await db.execute(
            select(FolderSyncSource).where(FolderSyncSource.folder_path == folder_path)
        )
        source = result.scalar_one_or_none()
        if not source or source.source_type not in _OAUTH_SOURCES:
            raise HTTPException(status_code=404, detail="OAuth sync source not found")

        cfg = _OAUTH_SOURCES[source.source_type]

        from ...services.sync.azure_devops import exchange_code_for_tokens as ado_exchange
        from ...services.sync.sharepoint import exchange_code_for_tokens as sp_exchange

        exchange_fn = ado_exchange if source.source_type == "azure_devops" else sp_exchange
        tokens = await exchange_fn(
            tenant_id=getattr(source, cfg["tenant_id"]),
            client_id=getattr(source, cfg["client_id"]),
            client_secret=getattr(source, cfg["client_secret"]),
            code=code,
            redirect_uri=_get_oauth_redirect_uri(),
        )
        setattr(source, cfg["refresh_token"], tokens["refresh_token"])

    from ...services.watcher import file_watcher
    await file_watcher.broadcast({
        "type": cfg["ws_event"],
        "path": folder_path,
    })

    browse_path = f"/browse/{folder_path}" if folder_path else "/browse"
    return RedirectResponse(url=browse_path, status_code=302)


# Legacy route so existing SharePoint bookmarks/tokens still work
@router.get("/sharepoint/callback")
async def sharepoint_oauth_callback_legacy(
    code: str = Query(...),
    state: str = Query(...),
):
    return await oauth_callback(code=code, state=state)


@router.get("/oauth/auth")
async def oauth_auth_initiate(
    folder_path: str = Query(...),
    user: CurrentUser = None,
    db: DB = None,
):
    """Unified OAuth2 auth initiation — dispatches by source_type."""
    result = await db.execute(
        select(FolderSyncSource).where(FolderSyncSource.folder_path == folder_path)
    )
    source = result.scalar_one_or_none()
    if not source or source.source_type not in _OAUTH_SOURCES:
        raise HTTPException(status_code=404, detail="OAuth sync source not found")

    cfg = _OAUTH_SOURCES[source.source_type]
    tenant_id = getattr(source, cfg["tenant_id"])
    client_id = getattr(source, cfg["client_id"])

    if not tenant_id or not client_id:
        raise HTTPException(
            status_code=400,
            detail="Save configuration (tenant ID, client ID, etc.) before connecting",
        )

    from ...services.sync.azure_devops import get_auth_url as ado_auth_url
    from ...services.sync.sharepoint import get_auth_url as sp_auth_url

    get_auth_url_fn = ado_auth_url if source.source_type == "azure_devops" else sp_auth_url
    state = base64.urlsafe_b64encode(source.folder_path.encode()).decode()

    auth_url = get_auth_url_fn(
        tenant_id=tenant_id,
        client_id=client_id,
        redirect_uri=_get_oauth_redirect_uri(),
        state=state,
    )

    return {"auth_url": auth_url}


# Legacy routes so existing JS still works
@router.get("/sharepoint/auth")
async def sharepoint_auth_initiate_legacy(
    folder_path: str = Query(...),
    user: CurrentUser = None,
    db: DB = None,
):
    return await oauth_auth_initiate(folder_path=folder_path, user=user, db=db)


@router.get("/azure-devops/auth")
async def azure_devops_auth_initiate_legacy(
    folder_path: str = Query(...),
    user: CurrentUser = None,
    db: DB = None,
):
    return await oauth_auth_initiate(folder_path=folder_path, user=user, db=db)


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

    if request.source_type not in ("sharepoint", "google_drive", "github", "azure_devops"):
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
        "ado_tenant_id", "ado_client_id", "ado_client_secret", "ado_refresh_token",
        "ado_organization", "ado_project", "ado_url",
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
    elif request.source_type == "azure_devops" and request.azure_devops:
        from ...services.sync.azure_devops import _parse_ado_url
        source.ado_tenant_id = request.azure_devops.tenant_id
        source.ado_client_id = request.azure_devops.client_id
        source.ado_client_secret = request.azure_devops.client_secret
        source.ado_url = request.azure_devops.url
        try:
            org, project = _parse_ado_url(request.azure_devops.url)
            source.ado_organization = org
            source.ado_project = project
        except ValueError:
            source.ado_organization = request.azure_devops.organization
            source.ado_project = request.azure_devops.project

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
    from ...services.watcher import file_watcher

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

            # Post-sync: fetch Teams meeting transcripts for SharePoint sources
            if source.source_type == "sharepoint":
                try:
                    from ...services.sync.teams_transcripts import fetch_transcripts_for_folder
                    token = await connector._get_access_token(source)
                    count = await fetch_transcripts_for_folder(source, fs, token)
                    if count:
                        logger.info("Fetched %d transcript(s) for %s", count, folder_path)
                except Exception as e:
                    logger.warning("Transcript fetch failed for %s: %s", folder_path, e)

            source.sync_status = "synced"
            source.sync_error = None
            source.last_synced_at = utc_now()
        except Exception as e:
            logger.exception("Sync failed for %s", folder_path)
            source.sync_status = "error"
            source.sync_error = str(e)

        # Broadcast sync status change via WebSocket
        await file_watcher.broadcast({
            "type": "sync_status",
            "path": folder_path,
            "sync_status": source.sync_status,
            "sync_error": source.sync_error,
            "last_synced_at": source.last_synced_at.isoformat() if source.last_synced_at else None,
        })
