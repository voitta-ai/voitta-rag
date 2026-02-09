"""Jira sync connector - Issues from Jira Server/Data Center."""

import hashlib
import json
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .base import BaseSyncConnector, RemoteFile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_jira_url(url: str) -> tuple[str, str]:
    """Parse a Jira URL into (base_url, project_key).

    Supports:
      https://jira.example.com/browse/PROJ
      https://jira.example.com/browse/PROJ-123
      https://jira.example.com/projects/PROJ
      https://jira.example.com (with separate project key)
    """
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    path_parts = [p for p in (parsed.path or "").strip("/").split("/") if p]

    project_key = ""
    if len(path_parts) >= 2:
        if path_parts[0] in ("browse", "projects"):
            # Extract project key from PROJ or PROJ-123
            key_part = path_parts[1]
            project_key = key_part.split("-")[0].upper()

    return base_url, project_key


def _sanitize_filename(name: str) -> str:
    """Sanitize a string for use as a filename/path component."""
    sanitized = re.sub(r'[<>:"/\\|?*]', "-", name)
    sanitized = re.sub(r"\s+", "-", sanitized)
    sanitized = re.sub(r"-{2,}", "-", sanitized)
    return sanitized.strip("-")[:100]


def _render_issue_md(issue: dict) -> str:
    """Render a Jira issue as a structured markdown document."""
    fields = issue.get("fields", {})
    key = issue["key"]
    issue_type = (fields.get("issuetype") or {}).get("name", "Unknown")
    summary = fields.get("summary", f"Issue {key}")
    status = (fields.get("status") or {}).get("name", "")
    priority = (fields.get("priority") or {}).get("name", "")
    assignee = (fields.get("assignee") or {}).get("displayName", "Unassigned")
    reporter = (fields.get("reporter") or {}).get("displayName", "Unknown")
    created = (fields.get("created") or "")[:10]
    updated = (fields.get("updated") or "")[:10]
    labels = fields.get("labels") or []
    components = [c.get("name", "") for c in (fields.get("components") or [])]

    # Epic link (varies by Jira version)
    epic_link = fields.get("parent", {}).get("key", "")
    if not epic_link:
        # Try common custom field names for epic link
        for field_name in ("customfield_10014", "customfield_10008", "epic"):
            if fields.get(field_name):
                epic_link = fields.get(field_name)
                if isinstance(epic_link, dict):
                    epic_link = epic_link.get("key", "")
                break

    lines: list[str] = []
    lines.append(f"# [{key}] {summary}\n")

    # Metadata table
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(f"| Type | {issue_type} |")
    lines.append(f"| Status | {status} |")
    lines.append(f"| Priority | {priority} |")
    lines.append(f"| Assignee | {assignee} |")
    lines.append(f"| Reporter | {reporter} |")
    lines.append(f"| Created | {created} |")
    lines.append(f"| Updated | {updated} |")
    if labels:
        lines.append(f"| Labels | {', '.join(labels)} |")
    if components:
        lines.append(f"| Components | {', '.join(components)} |")
    if epic_link:
        lines.append(f"| Epic | {epic_link} |")
    lines.append("")

    # Description
    description = fields.get("description") or ""
    if description:
        lines.append("## Description\n")
        lines.append(description)
        lines.append("")

    # Comments
    comments_data = fields.get("comment", {})
    comments = comments_data.get("comments", []) if isinstance(comments_data, dict) else []
    if comments:
        lines.append("## Comments\n")
        for comment in comments:
            author = (comment.get("author") or {}).get("displayName", "Unknown")
            date = (comment.get("created") or "")[:10]
            body = comment.get("body", "")
            lines.append(f"### {author} ({date})\n")
            lines.append(body)
            lines.append("")

    # Attachments (as links)
    attachments = fields.get("attachment") or []
    if attachments:
        lines.append("## Attachments\n")
        for att in attachments:
            name = att.get("filename", "attachment")
            url = att.get("content", "")
            size = att.get("size", 0)
            size_kb = size // 1024 if size else 0
            lines.append(f"- [{name}]({url}) ({size_kb} KB)")
        lines.append("")

    # Issue links
    issue_links = fields.get("issuelinks") or []
    if issue_links:
        lines.append("## Related Issues\n")
        for link in issue_links:
            link_type = (link.get("type") or {}).get("name", "Related")
            if link.get("outwardIssue"):
                related = link["outwardIssue"]
                direction = (link.get("type") or {}).get("outward", "relates to")
            elif link.get("inwardIssue"):
                related = link["inwardIssue"]
                direction = (link.get("type") or {}).get("inward", "is related to")
            else:
                continue
            related_key = related.get("key", "")
            related_summary = (related.get("fields") or {}).get("summary", "")
            lines.append(f"- {direction}: [{related_key}] {related_summary}")
        lines.append("")

    # Subtasks
    subtasks = fields.get("subtasks") or []
    if subtasks:
        lines.append("## Subtasks\n")
        for subtask in subtasks:
            st_key = subtask.get("key", "")
            st_summary = (subtask.get("fields") or {}).get("summary", "")
            st_status = ((subtask.get("fields") or {}).get("status") or {}).get("name", "")
            lines.append(f"- [{st_key}] {st_summary} ({st_status})")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class JiraConnector(BaseSyncConnector):

    def _headers(self, token: str) -> dict:
        """Build auth headers for Jira Server/DC with PAT."""
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _api_base(self, source) -> str:
        return f"{source.jira_url}/rest/api/2"

    # ---- list_files -------------------------------------------------------

    async def list_files(self, source) -> list[RemoteFile]:
        if not source.jira_token:
            raise RuntimeError("Jira token not configured")
        if not source.jira_project:
            raise RuntimeError("Jira project not configured")

        files: list[RemoteFile] = []
        headers = self._headers(source.jira_token)
        base = self._api_base(source)

        async with httpx.AsyncClient(timeout=60.0) as client:
            start_at = 0
            max_results = 100
            total = None

            while total is None or start_at < total:
                # JQL to get all issues in project
                jql = f"project = {source.jira_project} ORDER BY updated DESC"
                resp = await client.get(
                    f"{base}/search",
                    params={
                        "jql": jql,
                        "startAt": start_at,
                        "maxResults": max_results,
                        "fields": "key,issuetype,summary,updated",
                    },
                    headers=headers,
                )

                if resp.status_code == 401:
                    raise RuntimeError("Jira authentication failed. Check your token.")
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"Jira search failed ({resp.status_code}): {resp.text[:500]}"
                    )

                data = resp.json()
                total = data.get("total", 0)
                issues = data.get("issues", [])

                for issue in issues:
                    key = issue["key"]
                    fields = issue.get("fields", {})
                    issue_type = (fields.get("issuetype") or {}).get("name", "Other")
                    summary = fields.get("summary", f"Issue-{key}")
                    updated = fields.get("updated", "")

                    safe_summary = _sanitize_filename(summary)
                    safe_type = _sanitize_filename(issue_type)
                    remote_path = f"issues/{safe_type}/{key}-{safe_summary}.md"

                    # Use updated timestamp as content hash for change detection
                    content_hash = hashlib.sha256(updated.encode()).hexdigest()

                    files.append(
                        RemoteFile(
                            remote_path=remote_path,
                            size=0,
                            modified_at=updated,
                            content_hash=content_hash,
                        )
                    )

                start_at += max_results
                if not issues:
                    break

        logger.info("Listed %d issues from Jira project %s", len(files), source.jira_project)
        return files

    # ---- download_file ----------------------------------------------------

    async def download_file(self, source, remote_path: str, local_path: Path) -> None:
        if not source.jira_token:
            raise RuntimeError("Jira token not configured")

        # Parse issue key from path: issues/{Type}/{KEY}-summary.md
        match = re.match(r"issues/[^/]+/([A-Z]+-\d+)-.*\.md$", remote_path)
        if not match:
            raise RuntimeError(f"Cannot parse issue path: {remote_path}")

        issue_key = match.group(1)
        headers = self._headers(source.jira_token)
        base = self._api_base(source)

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{base}/issue/{issue_key}",
                params={
                    "fields": "*all",
                    "expand": "renderedFields",
                },
                headers=headers,
            )

            if resp.status_code == 401:
                raise RuntimeError("Jira authentication failed. Check your token.")
            if resp.status_code == 404:
                raise RuntimeError(f"Issue not found: {issue_key}")
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Failed to fetch issue {issue_key}: {resp.text[:300]}"
                )

            issue = resp.json()

        md = _render_issue_md(issue)
        local_path.write_text(md, encoding="utf-8")

    # ---- sync override (timestamp-based change tracking) -------------------

    async def sync(self, source, fs, keep_extensions: set[str] | None = None) -> dict:
        """Custom sync using timestamp-based change detection via sidecar file."""
        folder_path = source.folder_path
        local_root = fs._resolve_path(folder_path)
        local_root.mkdir(parents=True, exist_ok=True)

        hash_file = local_root / ".jira_revisions.json"
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
