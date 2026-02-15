"""SharePoint sync connector using Microsoft Graph API with delegated auth."""

import logging
import re
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

    async def sync(self, source, fs, keep_extensions: set[str] | None = None) -> dict:
        """Sync with .vtt files preserved across mirror deletions."""
        keep = keep_extensions or set()
        keep.add(".vtt")
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
