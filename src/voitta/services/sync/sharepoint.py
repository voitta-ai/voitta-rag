"""SharePoint sync connector using Microsoft Graph API with delegated auth."""

import asyncio
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
SHAREPOINT_SCOPES = "offline_access Sites.Read.All Files.Read.All User.Read.All GroupMember.Read.All OnlineMeetings.Read OnlineMeetingTranscript.Read.All"


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

    def __init__(self):
        # Populated during _list_recursive: {remote_path: (drive_id, item_id)}
        self._item_ids: dict[str, tuple[str, str]] = {}

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
                    self._item_ids[item_path] = (drive_id, item["id"])

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

    @staticmethod
    async def _graph_get(
        client: httpx.AsyncClient, url: str, headers: dict,
        *, max_retries: int = 4,
    ) -> httpx.Response:
        """GET with retry + backoff for Graph API 429 (throttling).

        Respects the Retry-After header when present. Falls back to
        exponential backoff (2, 4, 8, 16 s).
        """
        for attempt in range(max_retries + 1):
            resp = await client.get(url, headers=headers)
            if resp.status_code != 429:
                return resp
            retry_after = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
            retry_after = min(retry_after, 30)  # cap wait at 30 s
            logger.warning(
                "Graph API throttled (429), retry %d/%d in %ds: %s",
                attempt + 1, max_retries, retry_after, url[:120],
            )
            await asyncio.sleep(retry_after)
        return resp  # return last 429 response if all retries exhausted

    @staticmethod
    async def _graph_post(
        client: httpx.AsyncClient, url: str, headers: dict, json_body: dict,
        *, max_retries: int = 4,
    ) -> httpx.Response:
        """POST with retry + backoff for Graph API 429 (throttling)."""
        for attempt in range(max_retries + 1):
            resp = await client.post(url, headers=headers, json=json_body)
            if resp.status_code != 429:
                return resp
            retry_after = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
            retry_after = min(retry_after, 30)
            logger.warning(
                "Graph API throttled (429), retry %d/%d in %ds: POST %s",
                attempt + 1, max_retries, retry_after, url[:120],
            )
            await asyncio.sleep(retry_after)
        return resp

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
        index_folder: str = "",
    ) -> dict:
        """Sync a single SharePoint site into a local directory."""
        site_id = site_dict["id"]
        site_name = site_dict.get("displayName") or site_dict.get("name", site_id)
        drive_id = await self._resolve_drive_for_site(token, site_id)

        # Scope item_ids to this site (clear before listing)
        self._item_ids.clear()

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

        # Resolve and store per-file ACLs for this site
        try:
            await self._resolve_and_store_acl(
                token, self._item_ids, site_root_path, index_folder,
                site_id=site_id,
            )
        except Exception as e:
            logger.warning("ACL resolution failed for site %s: %s", site_name, e)

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
            site_index_folder = f"{folder_path}/sites/{safe_name}"
            try:
                stats = await self._sync_single_site(
                    source, token, site_dict, site_root, keep_extensions,
                    index_folder=site_index_folder,
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

    # ── ACL resolution ─────────────────────────────────────────────

    @staticmethod
    def _extract_users_from_permissions(permissions: list[dict]) -> set[str]:
        """Extract user identifiers from a permissions list.

        Returns Azure AD object IDs (email extraction requires User.Read.All
        which may not be available).
        """
        users: set[str] = set()
        for perm in permissions:
            # grantedToIdentitiesV2 — individual users or link recipients
            for identity in perm.get("grantedToIdentitiesV2", []):
                user = identity.get("user", {})
                uid = user.get("id") or ""
                email = user.get("email") or ""
                if email and "@" in email:
                    users.add(email.lower())
                elif uid and len(uid) == 36 and "-" in uid:
                    users.add(uid)

            # grantedToV2.user — single user grant
            granted_to = perm.get("grantedToV2", {})
            user_block = granted_to.get("user", {})
            if user_block:
                uid = user_block.get("id") or ""
                email = user_block.get("email") or ""
                if email and "@" in email:
                    users.add(email.lower())
                elif uid and len(uid) == 36 and "-" in uid:
                    users.add(uid)

        return users

    async def _fetch_site_members(
        self, client: httpx.AsyncClient, token: str, site_id: str
    ) -> list[str]:
        """Fetch all transitive members of the M365 group for a site.

        Uses /groups/{id}/transitiveMembers/microsoft.graph.user to resolve
        nested group memberships into a flat user list (includes owners).
        Handles pagination for large groups.
        Returns sorted list of lowercase email addresses.
        """
        headers = {"Authorization": f"Bearer {token}"}
        emails: set[str] = set()

        # Get the site display name to find the associated M365 group
        resp = await self._graph_get(
            client,
            f"https://graph.microsoft.com/v1.0/sites/{site_id}?$select=id,displayName",
            headers,
        )
        if resp.status_code != 200:
            logger.warning("Failed to fetch site %s: %s", site_id, resp.status_code)
            return []

        site_name = resp.json().get("displayName", "")

        # Search for the M365 group by site display name
        resp = await self._graph_get(
            client,
            "https://graph.microsoft.com/v1.0/groups"
            f"?$filter=displayName eq '{site_name}'"
            "&$select=id,displayName",
            headers,
        )
        if resp.status_code != 200 or not resp.json().get("value"):
            logger.warning("No M365 group found for site '%s'", site_name)
            return []

        group_id = resp.json()["value"][0]["id"]

        # Fetch all transitive members (includes nested groups, owners)
        url: str | None = (
            f"https://graph.microsoft.com/v1.0/groups/{group_id}"
            "/transitiveMembers/microsoft.graph.user"
            "?$select=mail,userPrincipalName"
        )
        while url:
            resp = await self._graph_get(client, url, headers)
            if resp.status_code != 200:
                logger.warning(
                    "Group %s transitiveMembers fetch failed: %s",
                    group_id, resp.status_code,
                )
                break
            data = resp.json()
            for member in data.get("value", []):
                email = member.get("mail") or member.get("userPrincipalName") or ""
                if email and "@" in email:
                    emails.add(email.lower())
            url = data.get("@odata.nextLink")

        logger.info(
            "Site '%s' group membership: %d users", site_name, len(emails)
        )
        return sorted(emails)

    async def _resolve_uuids_to_emails(
        self, client: httpx.AsyncClient, token: str, uuids: set[str]
    ) -> dict[str, str]:
        """Resolve Azure AD object IDs to email addresses via Graph API.

        Requires User.Read.All scope.
        Returns {uuid: email} for successfully resolved IDs.
        """
        resolved: dict[str, str] = {}
        headers = {"Authorization": f"Bearer {token}"}
        for uid in uuids:
            try:
                resp = await self._graph_get(
                    client,
                    f"https://graph.microsoft.com/v1.0/users/{uid}"
                    "?$select=mail,userPrincipalName",
                    headers,
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                email = data.get("mail") or data.get("userPrincipalName") or ""
                if email and "@" in email:
                    resolved[uid] = email.lower()
            except Exception:
                continue
        if resolved:
            logger.info("Resolved %d/%d UUIDs to emails", len(resolved), len(uuids))
        return resolved

    async def _batch_fetch_permissions(
        self, client: httpx.AsyncClient, token: str,
        item_ids: dict[str, tuple[str, str]],
    ) -> tuple[dict[str, list[dict]], list[str]]:
        """Fetch permissions for multiple files using Graph $batch API.

        Groups items into chunks of 20 (Graph $batch limit) and POSTs each
        chunk. Returns (successes: {path: [perms]}, failures: [paths]).
        """
        BATCH_SIZE = 20
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        successes: dict[str, list[dict]] = {}
        failures: list[str] = []

        items = list(item_ids.items())
        for chunk_start in range(0, len(items), BATCH_SIZE):
            chunk = items[chunk_start:chunk_start + BATCH_SIZE]
            # Map batch request id → remote_path for correlating responses
            id_to_path: dict[str, str] = {}
            requests = []
            for i, (remote_path, (drive_id, item_id)) in enumerate(chunk):
                req_id = str(i)
                id_to_path[req_id] = remote_path
                requests.append({
                    "id": req_id,
                    "method": "GET",
                    "url": f"/drives/{drive_id}/items/{item_id}/permissions",
                })

            resp = await self._graph_post(
                client,
                "https://graph.microsoft.com/v1.0/$batch",
                headers,
                {"requests": requests},
            )
            if resp.status_code != 200:
                logger.warning(
                    "Batch POST failed (%s), marking %d files as failures",
                    resp.status_code, len(chunk),
                )
                failures.extend(id_to_path.values())
                continue

            for response in resp.json().get("responses", []):
                path = id_to_path.get(response.get("id", ""))
                if not path:
                    continue
                status = response.get("status", 0)
                if status == 200:
                    body = response.get("body", {})
                    successes[path] = body.get("value", [])
                else:
                    logger.warning(
                        "Batch item failed for %s (status %s)", path, status,
                    )
                    failures.append(path)

        logger.info(
            "Batch permissions: %d succeeded, %d failed (%d batch calls)",
            len(successes), len(failures),
            (len(items) + BATCH_SIZE - 1) // BATCH_SIZE,
        )
        return successes, failures

    async def _fetch_file_acls(
        self, token: str, item_ids: dict[str, tuple[str, str]],
        site_id: str = "",
    ) -> dict[str, list[str]]:
        """Fetch per-file permissions and return {remote_path: [user_emails]}.

        Uses the Graph $batch API to fetch permissions in chunks of 20,
        resolves UUIDs to emails, and merges site membership into all files.
        """
        acl_map: dict[str, list[str]] = {}
        files_without_explicit_perms: list[str] = []

        async with httpx.AsyncClient() as client:
            # Batch-fetch all permissions
            successes, failures = await self._batch_fetch_permissions(
                client, token, item_ids,
            )
            files_without_explicit_perms.extend(failures)

            # Extract users from each file's permissions
            for remote_path, perms in successes.items():
                users = self._extract_users_from_permissions(perms)
                if users:
                    acl_map[remote_path] = sorted(users)
                else:
                    files_without_explicit_perms.append(remote_path)

            # Collect unresolved UUIDs and resolve them to emails
            all_uuids: set[str] = set()
            for users_list in acl_map.values():
                for u in users_list:
                    if "@" not in u and len(u) == 36 and "-" in u:
                        all_uuids.add(u)

            uuid_to_email: dict[str, str] = {}
            if all_uuids:
                uuid_to_email = await self._resolve_uuids_to_emails(
                    client, token, all_uuids
                )

            # Replace UUIDs with emails in the ACL map
            if uuid_to_email:
                for remote_path, users_list in acl_map.items():
                    acl_map[remote_path] = sorted({
                        uuid_to_email.get(u, u) for u in users_list
                    })

            # Site members have access to all files via inherited permissions.
            # Union site membership into every file's ACL.
            if site_id:
                logger.info("Fetching site membership for site_id=%s", site_id[:40])
                site_members = await self._fetch_site_members(
                    client, token, site_id
                )
                logger.info("Site members result: %d users", len(site_members))
                if site_members:
                    site_member_set = set(site_members)
                    # Add site members to files that already have explicit grants
                    for remote_path in list(acl_map):
                        merged = set(acl_map[remote_path]) | site_member_set
                        acl_map[remote_path] = sorted(merged)
                    # Set site members for files with no explicit grants
                    for remote_path in files_without_explicit_perms:
                        acl_map[remote_path] = site_members
                    logger.info(
                        "Merged site membership (%d users) into %d files",
                        len(site_members), len(acl_map),
                    )

        logger.info(
            "Fetched file-level ACLs: %d/%d files have permissions",
            len(acl_map), len(item_ids),
        )
        return acl_map

    @staticmethod
    def _inherit_acls_for_derived_files(acl_map: dict[str, list[str]]) -> None:
        """Propagate ACLs from .url files to their derived .vtt transcripts.

        Always overwrites the VTT entry with the .url ACL, since the .url
        is the authoritative source (VTTs have no explicit permissions).
        """
        url_entries = {k: v for k, v in acl_map.items() if k.endswith(".url")}
        for url_path, users in url_entries.items():
            vtt_path = url_path.rsplit(".url", 1)[0] + ".vtt"
            acl_map[vtt_path] = users

    def _write_acl_sidecar(self, local_root: Path, acl_map: dict[str, list[str]]) -> None:
        """Write .voitta_acl.json sidecar with per-file ACLs."""
        self._inherit_acls_for_derived_files(acl_map)
        acl_path = local_root / ".voitta_acl.json"
        acl_path.write_text(json.dumps(acl_map))
        logger.info("Wrote ACL sidecar to %s (%d files)", acl_path, len(acl_map))

    def _update_qdrant_acls(
        self, folder_path: str, acl_map: dict[str, list[str]]
    ) -> None:
        """Update per-file allowed_users in Qdrant."""
        from ..vector_store import get_vector_store

        vs = get_vector_store()
        updated = 0
        for remote_path, allowed_users in acl_map.items():
            file_path = f"{folder_path}/{remote_path}" if folder_path else remote_path
            vs.set_file_acl(file_path, allowed_users)
            updated += 1
        logger.info("Updated Qdrant ACL for %d files under '%s'", updated, folder_path)

    async def _resolve_and_store_acl(
        self, token: str, item_ids: dict[str, tuple[str, str]],
        local_root: Path, folder_path: str, site_id: str = ""
    ) -> None:
        """Fetch per-file permissions, write sidecar, and update Qdrant.

        If the new ACL map covers fewer files than the existing sidecar,
        merge previous data to avoid overwriting good ACLs with incomplete
        results (e.g. from Graph API throttling).
        """
        if not item_ids:
            return
        acl_map = await self._fetch_file_acls(token, item_ids, site_id=site_id)
        if not acl_map:
            return

        # Guard against overwriting a good sidecar with throttled/incomplete data
        existing_sidecar = local_root / ".voitta_acl.json"
        if existing_sidecar.exists():
            try:
                prev = json.loads(existing_sidecar.read_text())
                # If previous sidecar covered more files, merge — keep previous
                # entries that the new run missed (likely due to 429 throttling)
                missing = set(prev.keys()) - set(acl_map.keys())
                if missing:
                    logger.warning(
                        "ACL sidecar merge: %d files present in previous sidecar "
                        "but missing from current run — preserving previous ACLs",
                        len(missing),
                    )
                    for key in missing:
                        acl_map[key] = prev[key]
            except Exception:
                pass

        self._write_acl_sidecar(local_root, acl_map)
        self._update_qdrant_acls(folder_path, acl_map)

    async def sync(self, source, fs, keep_extensions: set[str] | None = None) -> dict:
        """Sync with .vtt files preserved across mirror deletions."""
        self._item_ids.clear()
        keep = keep_extensions or set()
        keep.add(".vtt")
        if getattr(source, "sp_all_sites", False) or getattr(source, "sp_selected_sites", None):
            result = await self._sync_multi_site(source, fs, keep)
        else:
            result = await super().sync(source, fs, keep_extensions=keep)

        # Resolve and store per-file ACLs after sync completes
        # (multi-site already handles ACLs per-site in _sync_single_site)
        is_multi_site = getattr(source, "sp_all_sites", False) or getattr(source, "sp_selected_sites", None)
        if not is_multi_site:
            try:
                token = await self._get_access_token(source)
                folder_path = source.folder_path
                local_root = fs._resolve_path(folder_path)
                # Resolve site_id for group membership lookup
                hostname, site_path, _ = _parse_sharepoint_url(source.sp_site_url)
                site_id = ""
                if site_path:
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(
                            f"https://graph.microsoft.com/v1.0/sites/{hostname}:{site_path}",
                            headers={"Authorization": f"Bearer {token}"},
                        )
                        if resp.status_code == 200:
                            site_id = resp.json()["id"]
                await self._resolve_and_store_acl(
                    token, self._item_ids, local_root, folder_path,
                    site_id=site_id,
                )
            except Exception as e:
                logger.warning("ACL resolution error: %s", e)

        return result

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
