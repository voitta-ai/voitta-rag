"""Google Drive sync connector."""

import asyncio
import json
import logging
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .base import BaseSyncConnector, RemoteFile

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

# Google Workspace native mimeTypes → (virtual_suffix, export_mimeType)
_GOOGLE_EXPORT_MAP = {
    "application/vnd.google-apps.document": (
        ".gdoc.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    "application/vnd.google-apps.spreadsheet": (
        ".gsheet.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    "application/vnd.google-apps.presentation": (
        ".gslides.pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
}

# Reverse lookup: virtual suffix → export mimeType (used by download)
_EXPORT_SUFFIXES = {ext: mime for ext, mime in _GOOGLE_EXPORT_MAP.values()}


# ---------------------------------------------------------------------------
# OAuth helpers (module-level, same pattern as Box)
# ---------------------------------------------------------------------------


def get_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Build the Google OAuth2 authorization URL."""
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": GOOGLE_DRIVE_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def list_root_folders(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """List root-level and shared-with-me folders in Google Drive."""
    async with httpx.AsyncClient() as client:
        # Refresh access token
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            },
        )
        if token_resp.status_code != 200:
            raise RuntimeError(f"Token refresh failed: {token_resp.text[:300]}")
        access_token = token_resp.json()["access_token"]

        headers = {"Authorization": f"Bearer {access_token}"}
        base_url = "https://www.googleapis.com/drive/v3/files"
        base_params = {
            "fields": "files(id,name)",
            "pageSize": "100",
            "orderBy": "name",
        }

        # My Drive root folders
        resp = await client.get(base_url, headers=headers, params={
            **base_params,
            "q": "'root' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
        })
        if resp.status_code != 200:
            raise RuntimeError(f"Drive API error: {resp.text[:300]}")
        my_folders = [{"id": f["id"], "name": f["name"]} for f in resp.json().get("files", [])]

        # Shared with me folders
        resp2 = await client.get(base_url, headers=headers, params={
            **base_params,
            "q": "sharedWithMe=true and mimeType='application/vnd.google-apps.folder' and trashed=false",
        })
        if resp2.status_code != 200:
            raise RuntimeError(f"Drive API error: {resp2.text[:300]}")
        shared_folders = [{"id": f["id"], "name": f["name"]} for f in resp2.json().get("files", [])]

        # Shared Drives (Team Drives)
        resp3 = await client.get(
            "https://www.googleapis.com/drive/v3/drives",
            headers=headers,
            params={"pageSize": "100"},
        )
        if resp3.status_code != 200:
            raise RuntimeError(f"Drive API error: {resp3.text[:300]}")
        shared_drives = [{"id": d["id"], "name": d["name"]} for d in resp3.json().get("drives", [])]

        return {"folders": my_folders, "shared_folders": shared_folders, "shared_drives": shared_drives}


async def exchange_code_for_tokens(
    client_id: str, client_secret: str, code: str, redirect_uri: str
) -> dict:
    """Exchange an authorization code for access + refresh tokens."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        if resp.status_code != 200:
            try:
                body = resp.json()
                error_desc = body.get("error_description", body.get("error", ""))
            except Exception:
                error_desc = resp.text[:500]
            raise RuntimeError(
                f"Google token exchange failed ({resp.status_code}): {error_desc}"
            )
        return resp.json()


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class GoogleDriveConnector(BaseSyncConnector):

    # In-memory token cache: folder_path -> (access_token, expires_at)
    _token_cache: dict[str, tuple[str, float]] = {}

    async def _get_access_token(self, source) -> str:
        """Get an OAuth access token, refreshing if needed."""
        if not source.gd_refresh_token:
            raise RuntimeError(
                "Google Drive not connected. Click 'Connect' to sign in via browser."
            )

        # Check memory cache
        cached = self._token_cache.get(source.folder_path)
        if cached:
            token, expires_at = cached
            if time.time() < expires_at - 60:
                return token

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": source.gd_client_id,
                    "client_secret": source.gd_client_secret,
                    "refresh_token": source.gd_refresh_token,
                },
            )
            if resp.status_code != 200:
                try:
                    body = resp.json()
                    error_desc = body.get("error_description", body.get("error", ""))
                except Exception:
                    error_desc = resp.text[:500]
                raise RuntimeError(
                    f"Google token refresh failed ({resp.status_code}): {error_desc}. "
                    "Try reconnecting Google Drive."
                )

            data = resp.json()

        # Google may rotate the refresh token (rare, but handle it)
        new_refresh = data.get("refresh_token")
        if new_refresh and new_refresh != source.gd_refresh_token:
            source.gd_refresh_token = new_refresh
            logger.info("Google refresh token rotated for %s", source.folder_path)

        access_token = data["access_token"]
        expires_in = data.get("expires_in", 3600)
        self._token_cache[source.folder_path] = (access_token, time.time() + expires_in)

        return access_token

    def _get_service_sa(self, source):
        """Build Drive service using service account credentials."""
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        creds_info = json.loads(source.gd_service_account_json)
        creds = Credentials.from_service_account_info(
            creds_info, scopes=[GOOGLE_DRIVE_SCOPE]
        )
        return build("drive", "v3", credentials=creds)

    def _get_service_oauth(self, access_token: str):
        """Build Drive service using an OAuth access token."""
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(token=access_token)
        return build("drive", "v3", credentials=creds)

    async def _get_service(self, source):
        """Dispatch: OAuth (if refresh_token) or service account."""
        if source.gd_refresh_token:
            token = await self._get_access_token(source)
            return self._get_service_oauth(token)
        elif source.gd_service_account_json:
            return self._get_service_sa(source)
        else:
            raise RuntimeError(
                "Google Drive not configured. Provide OAuth credentials or a service account."
            )

    async def list_files(self, source) -> list[RemoteFile]:
        service = await self._get_service(source)
        files: list[RemoteFile] = []
        await asyncio.to_thread(
            self._list_recursive_sync, service, source.gd_folder_id, "", files
        )
        return files

    def _list_recursive_sync(self, service, folder_id, current_path, files):
        page_token = None
        while True:
            results = (
                service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id, name, mimeType, size, modifiedTime, createdTime, md5Checksum)",
                    pageSize=1000,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )

            for item in results.get("files", []):
                mime = item["mimeType"]
                item_path = f"{current_path}/{item['name']}" if current_path else item["name"]
                if mime == "application/vnd.google-apps.folder":
                    self._list_recursive_sync(service, item["id"], item_path, files)
                elif mime in _GOOGLE_EXPORT_MAP:
                    suffix, _ = _GOOGLE_EXPORT_MAP[mime]
                    files.append(
                        RemoteFile(
                            remote_path=item_path + suffix,
                            size=0,  # native Google files have no size
                            modified_at=item.get("modifiedTime", ""),
                            content_hash=None,
                            created_at=item.get("createdTime", ""),
                        )
                    )
                elif mime.startswith("application/vnd.google-apps."):
                    # Skip other Google-native types (forms, sites, maps, etc.)
                    continue
                else:
                    files.append(
                        RemoteFile(
                            remote_path=item_path,
                            size=int(item.get("size", 0)),
                            modified_at=item.get("modifiedTime", ""),
                            content_hash=item.get("md5Checksum"),
                            created_at=item.get("createdTime", ""),
                        )
                    )

            page_token = results.get("nextPageToken")
            if not page_token:
                break

    async def download_file(self, source, remote_path: str, local_path: Path) -> None:
        service = await self._get_service(source)

        # Detect Google Workspace export files by their virtual suffix
        export_mime = None
        resolve_path = remote_path
        for suffix, mime in _EXPORT_SUFFIXES.items():
            if remote_path.endswith(suffix):
                export_mime = mime
                resolve_path = remote_path[: -len(suffix)]
                break

        file_id = await asyncio.to_thread(
            self._resolve_file_id, service, source.gd_folder_id, resolve_path
        )

        if export_mime:
            def _export():
                from googleapiclient.http import MediaIoBaseDownload

                request = service.files().export_media(fileId=file_id, mimeType=export_mime)
                with open(local_path, "wb") as f:
                    downloader = MediaIoBaseDownload(f, request)
                    done = False
                    while not done:
                        _, done = downloader.next_chunk()

            await asyncio.to_thread(_export)
        else:
            def _download():
                from googleapiclient.http import MediaIoBaseDownload

                request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
                with open(local_path, "wb") as f:
                    downloader = MediaIoBaseDownload(f, request)
                    done = False
                    while not done:
                        _, done = downloader.next_chunk()

            await asyncio.to_thread(_download)

    def _resolve_file_id(self, service, root_folder_id, remote_path):
        """Walk path segments to find the file's Drive ID."""
        parts = remote_path.split("/")
        current_parent = root_folder_id
        for part in parts:
            escaped = part.replace("'", "\\'")
            results = (
                service.files()
                .list(
                    q=f"'{current_parent}' in parents and name = '{escaped}' and trashed = false",
                    fields="files(id, mimeType)",
                    pageSize=1,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            found = results.get("files", [])
            if not found:
                raise FileNotFoundError(f"Remote path not found: {remote_path}")
            current_parent = found[0]["id"]
        return current_parent
