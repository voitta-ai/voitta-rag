"""Box sync connector using OAuth 2.0 delegated auth."""

import logging
import re
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .base import BaseSyncConnector, RemoteFile

logger = logging.getLogger(__name__)

BOX_AUTH_URL = "https://account.box.com/api/oauth2/authorize"
BOX_TOKEN_URL = "https://api.box.com/oauth2/token"
BOX_API_BASE = "https://api.box.com/2.0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sanitize_filename(name: str) -> str:
    """Sanitize a string for use as a filename/path component."""
    sanitized = re.sub(r'[<>:"/\\|?*]', "-", name)
    sanitized = re.sub(r"\s+", "-", sanitized)
    sanitized = re.sub(r"-{2,}", "-", sanitized)
    return sanitized.strip("-")[:100]


def get_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Build the Box OAuth2 authorization URL."""
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{BOX_AUTH_URL}?{urlencode(params)}"


async def exchange_code_for_tokens(
    client_id: str, client_secret: str, code: str, redirect_uri: str
) -> dict:
    """Exchange an authorization code for access + refresh tokens."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            BOX_TOKEN_URL,
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
                f"Box token exchange failed ({resp.status_code}): {error_desc}"
            )
        return resp.json()


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class BoxConnector(BaseSyncConnector):
    """Sync files from a Box folder using OAuth 2.0 delegated auth."""

    # In-memory token cache: folder_path -> (access_token, expires_at)
    _token_cache: dict[str, tuple[str, float]] = {}

    async def _get_access_token(self, source) -> str:
        """Get an access token, refreshing if needed.

        Box refresh tokens are single-use: each refresh returns a new
        refresh token that must be persisted immediately.
        """
        if not source.box_refresh_token:
            raise RuntimeError(
                "Box not connected. Click 'Connect' to sign in via browser."
            )

        # Check memory cache
        cached = self._token_cache.get(source.folder_path)
        if cached:
            token, expires_at = cached
            if time.time() < expires_at - 60:  # 60s safety margin
                return token

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                BOX_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": source.box_client_id,
                    "client_secret": source.box_client_secret,
                    "refresh_token": source.box_refresh_token,
                },
            )
            if resp.status_code != 200:
                try:
                    body = resp.json()
                    error_desc = body.get("error_description", body.get("error", ""))
                except Exception:
                    error_desc = resp.text[:500]
                raise RuntimeError(
                    f"Box token refresh failed ({resp.status_code}): {error_desc}. "
                    "Try reconnecting Box."
                )

            data = resp.json()

        # Box always rotates the refresh token
        new_refresh = data.get("refresh_token")
        if new_refresh and new_refresh != source.box_refresh_token:
            source.box_refresh_token = new_refresh
            logger.info("Box refresh token rotated for %s", source.folder_path)

        access_token = data["access_token"]
        expires_in = data.get("expires_in", 3600)
        self._token_cache[source.folder_path] = (access_token, time.time() + expires_in)

        return access_token

    async def list_files(self, source) -> list[RemoteFile]:
        """Recursively list all files in the configured Box folder."""
        if not source.box_folder_id:
            raise RuntimeError("Box folder ID not configured")

        token = await self._get_access_token(source)
        files: list[RemoteFile] = []

        async with httpx.AsyncClient(timeout=60.0) as client:
            await self._list_folder_recursive(
                client, token, source.box_folder_id, "", files
            )

        logger.info("Listed %d files from Box folder %s", len(files), source.box_folder_id)
        return files

    async def _list_folder_recursive(
        self, client: httpx.AsyncClient, token: str, folder_id: str, path_prefix: str,
        files: list[RemoteFile],
    ) -> None:
        """Walk a Box folder tree, collecting all files."""
        offset = 0
        limit = 1000

        while True:
            resp = await client.get(
                f"{BOX_API_BASE}/folders/{folder_id}/items",
                params={
                    "fields": "name,size,modified_at,created_at,sha1,type",
                    "limit": limit,
                    "offset": offset,
                },
                headers={"Authorization": f"Bearer {token}"},
            )

            if resp.status_code == 401:
                raise RuntimeError("Box authentication failed. Try reconnecting.")
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Box folder list failed ({resp.status_code}): {resp.text[:500]}"
                )

            data = resp.json()
            entries = data.get("entries", [])

            for entry in entries:
                entry_name = entry.get("name", "")
                entry_type = entry.get("type", "")
                entry_id = entry.get("id", "")

                if entry_type == "folder":
                    subfolder_path = f"{path_prefix}{entry_name}/"
                    await self._list_folder_recursive(
                        client, token, entry_id, subfolder_path, files
                    )
                elif entry_type == "file":
                    # Embed file ID in filename for reliable download
                    safe_name = _sanitize_filename(entry_name)
                    remote_path = f"{path_prefix}{entry_id}-{safe_name}"

                    files.append(
                        RemoteFile(
                            remote_path=remote_path,
                            size=entry.get("size", 0),
                            modified_at=entry.get("modified_at", ""),
                            content_hash=entry.get("sha1"),
                            created_at=entry.get("created_at", ""),
                        )
                    )

            total_count = data.get("total_count", 0)
            offset += len(entries)
            if offset >= total_count or len(entries) == 0:
                break

    async def download_file(self, source, remote_path: str, local_path: Path) -> None:
        """Download a file from Box by its embedded file ID."""
        # Extract file ID from path: {id}-{sanitized_name}
        stem = Path(remote_path).name
        m = re.match(r"^(\d+)-", stem)
        if not m:
            raise RuntimeError(f"Cannot parse Box file ID from path: {remote_path}")
        file_id = m.group(1)

        token = await self._get_access_token(source)

        async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
            resp = await client.get(
                f"{BOX_API_BASE}/files/{file_id}/content",
                headers={"Authorization": f"Bearer {token}"},
            )

            if resp.status_code == 401:
                raise RuntimeError("Box authentication failed. Try reconnecting.")
            if resp.status_code == 404:
                raise RuntimeError(f"Box file not found: {file_id}")
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Box download failed ({resp.status_code}): {resp.text[:500]}"
                )

            local_path.write_bytes(resp.content)
