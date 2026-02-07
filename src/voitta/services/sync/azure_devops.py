"""Azure DevOps sync connector - Work Items and Wiki pages."""

import hashlib
import json
import logging
import re
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

import httpx

from .base import BaseSyncConnector, RemoteFile

logger = logging.getLogger(__name__)

ADO_SCOPES = "499b84ac-1321-427f-aa17-267ca6975798/user_impersonation offline_access"
ADO_API_VERSION = "7.1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_ado_url(url: str) -> tuple[str, str]:
    """Parse an Azure DevOps URL into (organization, project).

    Supports:
      https://dev.azure.com/{org}/{project}
      https://{org}.visualstudio.com/{project}
    """
    parsed = urlparse(url)
    path_parts = [p for p in (parsed.path or "").strip("/").split("/") if p]

    hostname = parsed.hostname or ""
    if "dev.azure.com" in hostname:
        if len(path_parts) >= 2:
            return path_parts[0], path_parts[1]
    elif "visualstudio.com" in hostname:
        org = hostname.split(".")[0]
        if path_parts:
            return org, path_parts[0]

    raise ValueError(f"Cannot parse Azure DevOps URL: {url}")


def _sanitize_filename(name: str) -> str:
    """Sanitize a string for use as a filename/path component."""
    sanitized = re.sub(r'[<>:"/\\|?*]', "-", name)
    sanitized = re.sub(r"\s+", "-", sanitized)
    sanitized = re.sub(r"-{2,}", "-", sanitized)
    return sanitized.strip("-")[:100]


def _html_to_markdown(html: str) -> str:
    """Basic HTML-to-markdown conversion for Azure DevOps rich-text fields."""
    if not html:
        return ""
    text = html
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</?p>", "\n", text)
    text = re.sub(r"<strong>(.*?)</strong>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<b>(.*?)</b>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<em>(.*?)</em>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<i>(.*?)</i>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r'<a[^>]+href="([^"]*)"[^>]*>(.*?)</a>', r"[\2](\1)", text, flags=re.DOTALL)
    text = re.sub(r"<li>(.*?)</li>", r"- \1", text, flags=re.DOTALL)
    text = re.sub(r"</?[ou]l>", "", text)
    text = re.sub(r"</?div[^>]*>", "\n", text)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _render_work_item_md(wi: dict, comments: list) -> str:
    """Render a work item as a structured markdown document."""
    fields = wi.get("fields", {})
    wi_id = wi["id"]
    wi_type = fields.get("System.WorkItemType", "Unknown")
    title = fields.get("System.Title", f"WorkItem-{wi_id}")
    state = fields.get("System.State", "")
    assigned_to = (fields.get("System.AssignedTo") or {}).get("displayName", "Unassigned")
    area_path = fields.get("System.AreaPath", "")
    iteration_path = fields.get("System.IterationPath", "")
    priority = fields.get("Microsoft.VSTS.Common.Priority", "")
    created_date = fields.get("System.CreatedDate", "")[:10]
    changed_date = fields.get("System.ChangedDate", "")[:10]
    tags = fields.get("System.Tags", "")

    lines: list[str] = []
    lines.append(f"# [{wi_type} {wi_id}] {title}\n")

    # Metadata table
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(f"| Type | {wi_type} |")
    lines.append(f"| State | {state} |")
    lines.append(f"| Assigned To | {assigned_to} |")
    lines.append(f"| Area Path | {area_path} |")
    lines.append(f"| Iteration | {iteration_path} |")
    if priority:
        lines.append(f"| Priority | {priority} |")
    lines.append(f"| Created | {created_date} |")
    lines.append(f"| Updated | {changed_date} |")
    if tags:
        lines.append(f"| Tags | {tags} |")
    lines.append("")

    # Description
    description = fields.get("System.Description", "")
    if description:
        lines.append("## Description\n")
        lines.append(_html_to_markdown(description))
        lines.append("")

    # Acceptance Criteria (User Stories)
    acceptance = fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", "")
    if acceptance:
        lines.append("## Acceptance Criteria\n")
        lines.append(_html_to_markdown(acceptance))
        lines.append("")

    # Repro Steps (Bugs)
    repro = fields.get("Microsoft.VSTS.TCM.ReproSteps", "")
    if repro:
        lines.append("## Repro Steps\n")
        lines.append(_html_to_markdown(repro))
        lines.append("")

    # Comments
    if comments:
        lines.append("## Comments\n")
        for comment in comments:
            author = (comment.get("createdBy") or {}).get("displayName", "Unknown")
            date = comment.get("createdDate", "")[:10]
            text = _html_to_markdown(comment.get("text", ""))
            lines.append(f"### {author} ({date})\n")
            lines.append(text)
            lines.append("")

    # Related work items
    relations = wi.get("relations") or []
    wi_relations = [r for r in relations if "workitem" in (r.get("url") or "").lower()]
    if wi_relations:
        lines.append("## Related Work Items\n")
        for rel in wi_relations:
            rel_type = (rel.get("attributes") or {}).get("name", rel.get("rel", "Related"))
            rel_url = rel.get("url", "")
            rel_id_match = re.search(r"/workItems/(\d+)", rel_url)
            rel_id = rel_id_match.group(1) if rel_id_match else "?"
            lines.append(f"- {rel_type}: Work Item {rel_id}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# OAuth2 helpers (Azure AD — same identity provider as SharePoint)
# ---------------------------------------------------------------------------


def get_auth_url(tenant_id: str, client_id: str, redirect_uri: str, state: str) -> str:
    """Build the Azure AD OAuth2 authorization URL for Azure DevOps."""
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": ADO_SCOPES,
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
                "scope": ADO_SCOPES,
            },
        )
        if resp.status_code != 200:
            try:
                body = resp.json()
                error_desc = body.get("error_description", body.get("error", ""))
            except Exception:
                error_desc = resp.text[:500]
            raise RuntimeError(
                f"Azure DevOps token exchange failed ({resp.status_code}): {error_desc}"
            )
        return resp.json()


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class AzureDevOpsConnector(BaseSyncConnector):

    def __init__(self):
        self._cached_token: str | None = None
        self._token_expires_at: float = 0.0

    async def _get_access_token(self, source) -> str:
        """Get an access token, using cache when possible (~1h lifetime)."""
        import time

        # Return cached token if still valid (with 5-min safety margin)
        if self._cached_token and time.time() < self._token_expires_at - 300:
            return self._cached_token

        if not source.ado_refresh_token:
            raise RuntimeError(
                "Azure DevOps not connected. Click 'Connect' to sign in via browser."
            )

        url = f"https://login.microsoftonline.com/{source.ado_tenant_id}/oauth2/v2.0/token"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                data={
                    "grant_type": "refresh_token",
                    "client_id": source.ado_client_id,
                    "client_secret": source.ado_client_secret,
                    "refresh_token": source.ado_refresh_token,
                    "scope": ADO_SCOPES,
                },
            )
            if resp.status_code != 200:
                try:
                    body = resp.json()
                    error_desc = body.get("error_description", body.get("error", ""))
                except Exception:
                    error_desc = resp.text[:500]
                raise RuntimeError(
                    f"Azure DevOps token refresh failed ({resp.status_code}): {error_desc}. "
                    "Try reconnecting Azure DevOps."
                )

            data = resp.json()
            new_refresh = data.get("refresh_token")
            if new_refresh and new_refresh != source.ado_refresh_token:
                source.ado_refresh_token = new_refresh
                logger.info("Azure DevOps refresh token rotated for %s", source.folder_path)

            self._cached_token = data["access_token"]
            self._token_expires_at = time.time() + data.get("expires_in", 3600)
            return self._cached_token

    def _api_base(self, source) -> str:
        return f"https://dev.azure.com/{source.ado_organization}/{quote(source.ado_project, safe='')}/_apis"

    def _headers(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    # ---- list_files -------------------------------------------------------

    async def list_files(self, source) -> list[RemoteFile]:
        token = await self._get_access_token(source)
        files: list[RemoteFile] = []

        async with httpx.AsyncClient(timeout=60.0) as client:
            await self._list_work_items(client, token, source, files)
            await self._list_wiki_pages(client, token, source, files)

        return files

    async def _list_work_items(self, client, token, source, files):
        base = self._api_base(source)
        headers = self._headers(token)

        # WIQL to get all work item IDs
        resp = await client.post(
            f"{base}/wit/wiql?api-version={ADO_API_VERSION}",
            json={
                "query": (
                    "SELECT [System.Id] FROM WorkItems "
                    "WHERE [System.TeamProject] = @project "
                    "ORDER BY [System.ChangedDate] DESC"
                )
            },
            headers=headers,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"WIQL query failed ({resp.status_code}): {resp.text[:500]}")

        work_item_ids = [item["id"] for item in resp.json().get("workItems", [])]
        if not work_item_ids:
            return

        # Batch-get work items (max 200 per request)
        for batch_start in range(0, len(work_item_ids), 200):
            batch_ids = work_item_ids[batch_start : batch_start + 200]
            ids_csv = ",".join(str(i) for i in batch_ids)

            resp = await client.get(
                f"{base}/wit/workitems?ids={ids_csv}&$expand=all&api-version={ADO_API_VERSION}",
                headers=headers,
            )
            if resp.status_code != 200:
                logger.error("Failed to fetch work items batch: %s", resp.text[:300])
                continue

            for wi in resp.json().get("value", []):
                fields = wi.get("fields", {})
                wi_id = wi["id"]
                wi_type = fields.get("System.WorkItemType", "Unknown")
                title = fields.get("System.Title", f"WorkItem-{wi_id}")
                changed_date = fields.get("System.ChangedDate", "")
                rev = str(wi.get("rev", 0))

                safe_title = _sanitize_filename(title)
                remote_path = f"work-items/{wi_type}/{wi_id}-{safe_title}.md"

                content_hash = hashlib.sha256(f"{rev}:{changed_date}".encode()).hexdigest()

                files.append(
                    RemoteFile(
                        remote_path=remote_path,
                        size=0,
                        modified_at=changed_date,
                        content_hash=content_hash,
                    )
                )

    async def _list_wiki_pages(self, client, token, source, files):
        base = self._api_base(source)
        headers = self._headers(token)

        resp = await client.get(
            f"{base}/wiki/wikis?api-version={ADO_API_VERSION}",
            headers=headers,
        )
        if resp.status_code != 200:
            logger.warning("Failed to list wikis: %s %s", resp.status_code, resp.text[:300])
            return

        for wiki in resp.json().get("value", []):
            wiki_id = wiki["id"]
            wiki_name = wiki.get("name", wiki_id)

            resp2 = await client.get(
                f"{base}/wiki/wikis/{wiki_id}/pages"
                f"?recursionLevel=full&api-version={ADO_API_VERSION}",
                headers=headers,
            )
            if resp2.status_code != 200:
                logger.warning("Failed to get wiki pages for %s: %s", wiki_name, resp2.text[:300])
                continue

            root_page = resp2.json()
            self._walk_wiki_tree(root_page, f"wiki/{_sanitize_filename(wiki_name)}", files)

    def _walk_wiki_tree(self, page: dict, base_path: str, files: list[RemoteFile]):
        page_path = page.get("path", "")
        git_item_path = page.get("gitItemPath", "")

        if page_path and page_path != "/":
            clean_path = page_path.strip("/")
            remote_path = f"{base_path}/{clean_path}.md"
            content_hash = hashlib.sha256(
                (git_item_path or page_path).encode()
            ).hexdigest()

            files.append(
                RemoteFile(
                    remote_path=remote_path,
                    size=0,
                    modified_at="",
                    content_hash=content_hash,
                )
            )

        for sub_page in page.get("subPages", []):
            self._walk_wiki_tree(sub_page, base_path, files)

    # ---- download_file ----------------------------------------------------

    async def download_file(self, source, remote_path: str, local_path: Path) -> None:
        token = await self._get_access_token(source)

        if remote_path.startswith("work-items/"):
            await self._download_work_item(source, token, remote_path, local_path)
        elif remote_path.startswith("wiki/"):
            await self._download_wiki_page(source, token, remote_path, local_path)
        else:
            raise RuntimeError(f"Unknown remote path pattern: {remote_path}")

    async def _download_work_item(self, source, token, remote_path, local_path):
        match = re.match(r"work-items/[^/]+/(\d+)-.*\.md$", remote_path)
        if not match:
            raise RuntimeError(f"Cannot parse work item path: {remote_path}")

        wi_id = int(match.group(1))
        base = self._api_base(source)
        headers = self._headers(token)

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{base}/wit/workitems/{wi_id}?$expand=all&api-version={ADO_API_VERSION}",
                headers=headers,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Failed to fetch work item {wi_id}: {resp.text[:300]}")
            wi = resp.json()

            comments: list = []
            resp = await client.get(
                f"{base}/wit/workitems/{wi_id}/comments"
                f"?api-version={ADO_API_VERSION}-preview.4",
                headers=headers,
            )
            if resp.status_code == 200:
                comments = resp.json().get("comments", [])

        md = _render_work_item_md(wi, comments)
        local_path.write_text(md, encoding="utf-8")

    async def _download_wiki_page(self, source, token, remote_path, local_path):
        # Parse: wiki/{WikiName}/{rest/of/path}.md
        parts = remote_path.split("/", 2)
        if len(parts) < 3:
            raise RuntimeError(f"Cannot parse wiki path: {remote_path}")

        wiki_name = parts[1]
        page_path = "/" + parts[2].removesuffix(".md")

        base = self._api_base(source)
        headers = self._headers(token)

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{base}/wiki/wikis?api-version={ADO_API_VERSION}",
                headers=headers,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Failed to list wikis: {resp.text[:300]}")

            wiki_id = None
            for wiki in resp.json().get("value", []):
                if _sanitize_filename(wiki.get("name", "")) == wiki_name:
                    wiki_id = wiki["id"]
                    break

            if not wiki_id:
                raise RuntimeError(f"Wiki not found: {wiki_name}")

            encoded_path = quote(page_path, safe="/")
            resp = await client.get(
                f"{base}/wiki/wikis/{wiki_id}/pages"
                f"?path={encoded_path}&includeContent=true"
                f"&api-version={ADO_API_VERSION}",
                headers=headers,
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Failed to fetch wiki page {page_path}: {resp.text[:300]}"
                )

            content = resp.json().get("content", "")

        local_path.write_text(content, encoding="utf-8")

    # ---- sync override (revision-based change tracking) -------------------

    async def sync(self, source, fs, keep_extensions: set[str] | None = None) -> dict:
        """Custom sync using revision-based change detection via sidecar file."""
        folder_path = source.folder_path
        local_root = fs._resolve_path(folder_path)
        local_root.mkdir(parents=True, exist_ok=True)

        hash_file = local_root / ".ado_revisions.json"
        old_revisions: dict[str, str] = {}
        if hash_file.exists():
            try:
                old_revisions = json.loads(hash_file.read_text())
            except Exception:
                pass

        remote_files = await self.list_files(source)
        remote_paths: set[str] = set()
        new_revisions: dict[str, str] = {}
        stats = {"downloaded": 0, "deleted": 0, "skipped": 0, "errors": 0}

        for rf in remote_files:
            remote_paths.add(rf.remote_path)
            new_revisions[rf.remote_path] = rf.content_hash or ""
            local_file = local_root / rf.remote_path

            if local_file.exists() and old_revisions.get(rf.remote_path) == rf.content_hash:
                stats["skipped"] += 1
                continue

            local_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                await self.download_file(source, rf.remote_path, local_file)
                stats["downloaded"] += 1
                logger.info("Downloaded: %s", rf.remote_path)
            except Exception as e:
                logger.error("Failed to download %s: %s", rf.remote_path, e)
                stats["errors"] += 1

        # Mirror-delete
        for local_file in local_root.rglob("*"):
            if local_file.is_file() and not local_file.name.startswith("."):
                rel = str(local_file.relative_to(local_root))
                if rel not in remote_paths:
                    try:
                        local_file.unlink()
                        stats["deleted"] += 1
                        logger.info("Deleted (not on remote): %s", rel)
                    except Exception as e:
                        logger.error("Failed to delete %s: %s", rel, e)
                        stats["errors"] += 1

        # Clean up empty directories
        for dirpath in sorted(local_root.rglob("*"), reverse=True):
            if dirpath.is_dir() and not any(dirpath.iterdir()):
                try:
                    dirpath.rmdir()
                except Exception:
                    pass

        hash_file.write_text(json.dumps(new_revisions), encoding="utf-8")
        logger.info("Sync complete for %s: %s", folder_path, stats)
        return stats
