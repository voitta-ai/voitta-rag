"""SharePoint sync connector using Microsoft Graph API with delegated auth."""

import hashlib
import json
import logging
import re
import shutil
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from .base import BaseSyncConnector, RemoteFile

logger = logging.getLogger(__name__)

# Scopes needed for delegated access
SHAREPOINT_SCOPES = "offline_access Sites.Read.All Files.Read.All OnlineMeetings.Read OnlineMeetingTranscript.Read.All"


def _parse_sharepoint_url(url: str) -> tuple[str, str, str]:
    """Parse a SharePoint URL into (hostname, site_path, drive_path).

    Handles various URL formats:
      https://tenant.sharepoint.com/sites/MySite
      https://tenant.sharepoint.com/sites/MySite/Shared Documents/folder
      https://tenant.sharepoint.com/sites/MySite/Shared%20Documents/Forms/AllItems.aspx
      https://tenant.sharepoint.com/teams/MyTeam/Documents/subfolder

    Returns:
        hostname: e.g. "tenant.sharepoint.com"
        site_path: e.g. "/sites/MySite" (just the site portion)
        drive_path: subfolder within the drive to scope listing, e.g. "folder"
                    empty string means list from drive root
    """
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    full_path = unquote(parsed.path).rstrip("/")

    # Extract site path: /sites/{name} or /teams/{name}
    site_match = re.match(r"(/(?:sites|teams)/[^/]+)", full_path)
    if site_match:
        site_path = site_match.group(1)
        remainder = full_path[len(site_path):].lstrip("/")
    else:
        site_path = ""
        remainder = full_path.lstrip("/")

    drive_path = ""
    if remainder:
        remainder = re.sub(r"/Forms/[^/]*\.aspx$", "", remainder)
        remainder = remainder.rstrip("/")
        parts = remainder.split("/")
        if len(parts) > 1:
            drive_path = "/".join(parts[1:])

    return hostname, site_path, drive_path


def _extract_graph_error(resp: httpx.Response) -> str:
    """Extract a human-readable error from a Graph API error response."""
    try:
        body = resp.json()
        error = body.get("error", {})
        code = error.get("code", "")
        message = error.get("message", "")
        if code or message:
            return f"{code}: {message}" if code else message
    except Exception:
        pass
    return resp.text[:500] if resp.text else str(resp.status_code)


def _raise_graph_error(resp: httpx.Response, context: str) -> None:
    """Raise a descriptive error for failed Graph API calls."""
    detail = _extract_graph_error(resp)
    hint = ""
    if resp.status_code == 401:
        hint = " (Token may be expired - try reconnecting SharePoint)"
    elif resp.status_code == 403:
        hint = " (Access denied - ensure the user has access to this site)"
    raise RuntimeError(f"SharePoint {context} failed ({resp.status_code}): {detail}{hint}")


def get_auth_url(tenant_id: str, client_id: str, redirect_uri: str, state: str) -> str:
    """Build the Microsoft OAuth2 authorization URL."""
    from urllib.parse import urlencode

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": SHAREPOINT_SCOPES,
        "state": state,
    }
    return (
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize?"
        + urlencode(params)
    )


async def exchange_code_for_tokens(
    tenant_id: str, client_id: str, client_secret: str, code: str, redirect_uri: str
) -> dict:
    """Exchange an authorization code for access + refresh tokens."""
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "scope": SHAREPOINT_SCOPES,
            },
        )
        if resp.status_code != 200:
            try:
                body = resp.json()
                error_desc = body.get("error_description", body.get("error", ""))
            except Exception:
                error_desc = resp.text[:500]
            raise RuntimeError(
                f"SharePoint token exchange failed ({resp.status_code}): {error_desc}"
            )
        return resp.json()


def _sanitize_site_name(name: str) -> str:
    """Replace filesystem-unsafe chars and whitespace with hyphens, truncate to 80 chars."""
    safe = re.sub(r'[^\w\-.]', '-', name)
    safe = re.sub(r'-{2,}', '-', safe).strip('-')
    return safe[:80] or "site"


async def list_sites(
    tenant_id: str, client_id: str, client_secret: str, refresh_token: str
) -> list[dict]:
    """List all SharePoint sites accessible to the user."""
    # Get access token via refresh token
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            token_url,
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "scope": SHAREPOINT_SCOPES,
            },
        )
        if resp.status_code != 200:
            _raise_graph_error(resp, "token refresh for site listing")
        access_token = resp.json()["access_token"]

    # Fetch all sites (paginated)
    sites = []
    url = "https://graph.microsoft.com/v1.0/sites?search=*"
    async with httpx.AsyncClient() as client:
        while url:
            resp = await client.get(
                url, headers={"Authorization": f"Bearer {access_token}"}
            )
            if resp.status_code != 200:
                _raise_graph_error(resp, "list sites")
            data = resp.json()
            for site in data.get("value", []):
                sites.append({
                    "id": site["id"],
                    "name": site.get("name", ""),
                    "displayName": site.get("displayName", site.get("name", "")),
                    "webUrl": site.get("webUrl", ""),
                })
            url = data.get("@odata.nextLink")

    sites.sort(key=lambda s: (s.get("displayName") or "").lower())
    return sites


class SharePointConnector(BaseSyncConnector):

    async def _get_access_token(self, source) -> str:
        """Get an access token using the stored refresh token (delegated auth)."""
        if not source.sp_refresh_token:
            raise RuntimeError(
                "SharePoint not connected. Click 'Connect' to sign in via browser."
            )

        url = f"https://login.microsoftonline.com/{source.sp_tenant_id}/oauth2/v2.0/token"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                data={
                    "grant_type": "refresh_token",
                    "client_id": source.sp_client_id,
                    "client_secret": source.sp_client_secret,
                    "refresh_token": source.sp_refresh_token,
                    "scope": SHAREPOINT_SCOPES,
                },
            )
            if resp.status_code != 200:
                try:
                    body = resp.json()
                    error_desc = body.get("error_description", body.get("error", ""))
                except Exception:
                    error_desc = resp.text[:500]
                raise RuntimeError(
                    f"SharePoint token refresh failed ({resp.status_code}): {error_desc}. "
                    "Try reconnecting SharePoint."
                )

            data = resp.json()

            # Microsoft may rotate the refresh token — update it if a new one is returned
            new_refresh = data.get("refresh_token")
            if new_refresh and new_refresh != source.sp_refresh_token:
                source.sp_refresh_token = new_refresh
                logger.info("SharePoint refresh token rotated for %s", source.folder_path)

            return data["access_token"]

    async def _resolve_site_and_drive(
        self, source, token: str
    ) -> tuple[str, str]:
        """Resolve (drive_id, drive_subfolder) from the site URL."""
        hostname, site_path, drive_path = _parse_sharepoint_url(source.sp_site_url)

        logger.info(
            "Parsed SharePoint URL: hostname=%s site_path=%s drive_path=%s",
            hostname, site_path, drive_path,
        )

        if source.sp_drive_id:
            return source.sp_drive_id, drive_path

        async with httpx.AsyncClient() as client:
            if site_path:
                site_url = f"https://graph.microsoft.com/v1.0/sites/{hostname}:{site_path}"
            else:
                site_url = f"https://graph.microsoft.com/v1.0/sites/{hostname}"

            resp = await client.get(
                site_url,
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                _raise_graph_error(resp, f"site lookup ({site_url})")
            site_id = resp.json()["id"]

            resp = await client.get(
                f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                _raise_graph_error(resp, "drive lookup")
            drive_id = resp.json()["id"]

        return drive_id, drive_path

    async def list_files(self, source) -> list[RemoteFile]:
        token = await self._get_access_token(source)
        drive_id, drive_path = await self._resolve_site_and_drive(source, token)
        files: list[RemoteFile] = []

        async with httpx.AsyncClient() as client:
            await self._list_recursive(client, token, drive_id, drive_path, files)

        if drive_path:
            prefix = drive_path + "/"
            for f in files:
                if f.remote_path.startswith(prefix):
                    f.remote_path = f.remote_path[len(prefix):]

        return files

    async def _list_recursive(self, client, token, drive_id, path, files):
        if not path:
            url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root/children"
        else:
            url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:/{path}:/children"

        while url:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code != 200:
                _raise_graph_error(resp, f"list files at '{path or 'root'}'")
            data = resp.json()

            for item in data.get("value", []):
                item_path = f"{path}/{item['name']}" if path else item["name"]
                if "folder" in item:
                    await self._list_recursive(client, token, drive_id, item_path, files)
                elif "file" in item:
                    files.append(
                        RemoteFile(
                            remote_path=item_path,
                            size=item.get("size", 0),
                            modified_at=item.get("lastModifiedDateTime", ""),
                            content_hash=item.get("file", {})
                            .get("hashes", {})
                            .get("sha256Hash"),
                            created_at=item.get("createdDateTime", ""),
                        )
                    )

            url = data.get("@odata.nextLink")

    async def _resolve_drive_for_site(self, token: str, site_id: str) -> str:
        """Get the default drive ID for a site."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                _raise_graph_error(resp, f"drive lookup for site {site_id}")
            return resp.json()["id"]

    async def _download_file_with_drive(
        self, token: str, drive_id: str, remote_path: str, local_path: Path
    ) -> None:
        """Download a file given a known drive_id (bypasses _resolve_site_and_drive)."""
        url = (
            f"https://graph.microsoft.com/v1.0/drives/{drive_id}"
            f"/root:/{remote_path}:/content"
        )
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code != 200:
                _raise_graph_error(resp, f"download '{remote_path}'")
            local_path.write_bytes(resp.content)

    async def _sync_single_site(
        self,
        source,
        token: str,
        site_dict: dict,
        site_root_path: Path,
        keep_extensions: set[str],
    ) -> dict:
        """Sync a single SharePoint site into a local directory."""
        site_id = site_dict["id"]
        site_name = site_dict.get("displayName") or site_dict.get("name", site_id)
        drive_id = await self._resolve_drive_for_site(token, site_id)

        # List remote files
        files: list[RemoteFile] = []
        async with httpx.AsyncClient() as client:
            await self._list_recursive(client, token, drive_id, "", files)

        remote_paths = set()
        stats = {"downloaded": 0, "deleted": 0, "skipped": 0, "errors": 0}
        site_root_path.mkdir(parents=True, exist_ok=True)

        for rf in files:
            remote_paths.add(rf.remote_path)
            local_file = site_root_path / rf.remote_path

            if local_file.exists():
                if rf.content_hash:
                    local_hash = hashlib.sha256(local_file.read_bytes()).hexdigest()
                    if local_hash == rf.content_hash:
                        stats["skipped"] += 1
                        continue
                elif local_file.stat().st_size == rf.size:
                    stats["skipped"] += 1
                    continue

            local_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                await self._download_file_with_drive(
                    token, drive_id, rf.remote_path, local_file
                )
                stats["downloaded"] += 1
                logger.info("Downloaded [%s]: %s", site_name, rf.remote_path)
            except Exception as e:
                logger.error("Failed to download [%s] %s: %s", site_name, rf.remote_path, e)
                stats["errors"] += 1

        # Mirror-delete local files not on remote
        for local_file in site_root_path.rglob("*"):
            if local_file.is_file() and not local_file.name.startswith("."):
                if local_file.suffix.lower() in keep_extensions:
                    continue
                rel = str(local_file.relative_to(site_root_path))
                if rel not in remote_paths:
                    try:
                        local_file.unlink()
                        stats["deleted"] += 1
                        logger.info("Deleted (not on remote) [%s]: %s", site_name, rel)
                    except Exception as e:
                        logger.error("Failed to delete [%s] %s: %s", site_name, rel, e)
                        stats["errors"] += 1

        # Clean up empty directories
        for dirpath in sorted(site_root_path.rglob("*"), reverse=True):
            if dirpath.is_dir() and not any(dirpath.iterdir()):
                try:
                    dirpath.rmdir()
                except Exception:
                    pass

        # Write timestamps sidecar per site
        timestamps = {}
        for rf in files:
            entry = {}
            if rf.modified_at:
                entry["modified_at"] = rf.modified_at
            if rf.created_at:
                entry["created_at"] = rf.created_at
            if entry:
                timestamps[rf.remote_path] = entry
        (site_root_path / ".voitta_timestamps.json").write_text(json.dumps(timestamps))

        logger.info("Site sync complete [%s]: %s", site_name, stats)
        return stats

    async def _sync_multi_site(
        self, source, fs, keep_extensions: set[str]
    ) -> dict:
        """Sync multiple SharePoint sites into sites/<name>/ subfolders."""
        token = await self._get_access_token(source)

        # Determine which sites to sync
        if getattr(source, "sp_all_sites", False):
            sites_to_sync = await list_sites(
                source.sp_tenant_id,
                source.sp_client_id,
                source.sp_client_secret,
                source.sp_refresh_token,
            )
        else:
            selected_json = getattr(source, "sp_selected_sites", None) or "[]"
            try:
                sites_to_sync = json.loads(selected_json)
            except (json.JSONDecodeError, TypeError):
                sites_to_sync = []

        if not sites_to_sync:
            raise RuntimeError("No SharePoint sites selected for sync")

        folder_path = source.folder_path
        local_root = fs._resolve_path(folder_path)
        sites_dir = local_root / "sites"
        sites_dir.mkdir(parents=True, exist_ok=True)

        totals = {"downloaded": 0, "deleted": 0, "skipped": 0, "errors": 0}

        synced_folder_names = set()
        for site_dict in sites_to_sync:
            safe_name = _sanitize_site_name(
                site_dict.get("displayName") or site_dict.get("name", "site")
            )
            synced_folder_names.add(safe_name)
            site_root = sites_dir / safe_name
            try:
                stats = await self._sync_single_site(
                    source, token, site_dict, site_root, keep_extensions
                )
                for k in totals:
                    totals[k] += stats[k]
            except Exception as e:
                logger.error(
                    "Failed to sync site %s for %s: %s",
                    site_dict.get("displayName", "?"), folder_path, e,
                )
                totals["errors"] += 1

        # Clean up stale site folders no longer in selection
        if sites_dir.exists():
            for child in list(sites_dir.iterdir()):
                if child.is_dir() and child.name not in synced_folder_names:
                    logger.info("Removing stale site folder: %s", child.name)
                    shutil.rmtree(child)

        logger.info("Multi-site sync complete for %s: %s", folder_path, totals)
        return totals

    async def sync(self, source, fs, keep_extensions: set[str] | None = None) -> dict:
        """Sync with .vtt files preserved across mirror deletions."""
        keep = keep_extensions or set()
        keep.add(".vtt")
        if getattr(source, "sp_all_sites", False) or getattr(source, "sp_selected_sites", None):
            return await self._sync_multi_site(source, fs, keep)
        return await super().sync(source, fs, keep_extensions=keep)

    async def download_file(self, source, remote_path: str, local_path: Path) -> None:
        token = await self._get_access_token(source)
        drive_id, drive_path = await self._resolve_site_and_drive(source, token)

        full_remote = f"{drive_path}/{remote_path}" if drive_path else remote_path

        url = (
            f"https://graph.microsoft.com/v1.0/drives/{drive_id}"
            f"/root:/{full_remote}:/content"
        )

        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code != 200:
                _raise_graph_error(resp, f"download '{remote_path}'")
            local_path.write_bytes(resp.content)
