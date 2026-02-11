"""Jira sync connector - Issues, boards, and sprints from Jira Server/Data Center."""

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


def _format_custom_value(value) -> str:
    """Format an arbitrary Jira field value for markdown display."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(
                    item.get("name", item.get("value", item.get("displayName", str(item))))
                )
            else:
                parts.append(str(item))
        return ", ".join(parts) if parts else ""
    if isinstance(value, dict):
        return value.get("name", value.get("value", value.get("displayName", str(value))))
    return str(value)


# Custom field IDs commonly used for epic link (handled separately)
_EPIC_CUSTOM_FIELDS = {"customfield_10014", "customfield_10008"}


def _render_issue_md(
    issue: dict,
    *,
    field_map: dict[str, str] | None = None,
) -> str:
    """Render a Jira issue as a structured markdown document.

    field_map: maps logical names to custom field IDs, e.g.
        {"sprint": "customfield_10007", "story_points": "customfield_10028"}
    """
    field_map = field_map or {}
    fields = issue.get("fields", {})
    changelog = issue.get("changelog", {})
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

    resolution_raw = fields.get("resolution")
    resolution = (
        resolution_raw.get("name", "") if isinstance(resolution_raw, dict) else (resolution_raw or "")
    )
    resolution_date = (fields.get("resolutiondate") or "")[:10] if fields.get("resolutiondate") else ""
    due_date = fields.get("duedate") or ""
    fix_versions = [v.get("name", "") for v in (fields.get("fixVersions") or [])]
    affects_versions = [v.get("name", "") for v in (fields.get("versions") or [])]
    environment = fields.get("environment") or ""
    votes = fields.get("votes")
    watches = fields.get("watches")
    security = fields.get("security")
    time_tracking = fields.get("timetracking") or {}

    # Epic link (varies by Jira version)
    epic_link = (fields.get("parent") or {}).get("key", "")
    if not epic_link:
        for fid in list(_EPIC_CUSTOM_FIELDS) + [field_map.get("epic", "")]:
            if fid and fields.get(fid):
                epic_link = fields.get(fid)
                if isinstance(epic_link, dict):
                    epic_link = epic_link.get("key", "")
                break

    # Sprint (from discovered custom field)
    sprint_field_id = field_map.get("sprint", "")
    sprint_value = ""
    if sprint_field_id and fields.get(sprint_field_id):
        sprint_value = _format_custom_value(fields[sprint_field_id])

    # Story points (from discovered custom field)
    sp_field_id = field_map.get("story_points", "")
    story_points = ""
    if sp_field_id and fields.get(sp_field_id):
        story_points = _format_custom_value(fields[sp_field_id])

    # Track consumed custom fields so we can dump the rest
    consumed_custom = set(_EPIC_CUSTOM_FIELDS)
    if sprint_field_id:
        consumed_custom.add(sprint_field_id)
    if sp_field_id:
        consumed_custom.add(sp_field_id)

    # ----- Build markdown -----
    lines: list[str] = []
    lines.append(f"# [{key}] {summary}\n")

    # Metadata table
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(f"| Type | {issue_type} |")
    lines.append(f"| Status | {status} |")
    lines.append(f"| Priority | {priority} |")
    if resolution:
        lines.append(f"| Resolution | {resolution} |")
    if resolution_date:
        lines.append(f"| Resolution Date | {resolution_date} |")
    lines.append(f"| Assignee | {assignee} |")
    lines.append(f"| Reporter | {reporter} |")
    lines.append(f"| Created | {created} |")
    lines.append(f"| Updated | {updated} |")
    if due_date:
        lines.append(f"| Due Date | {due_date} |")
    if labels:
        lines.append(f"| Labels | {', '.join(labels)} |")
    if components:
        lines.append(f"| Components | {', '.join(components)} |")
    if epic_link:
        lines.append(f"| Epic | {epic_link} |")
    if sprint_value:
        lines.append(f"| Sprint | {sprint_value} |")
    if story_points:
        lines.append(f"| Story Points | {story_points} |")
    if fix_versions:
        lines.append(f"| Fix Version/s | {', '.join(fix_versions)} |")
    if affects_versions:
        lines.append(f"| Affects Version/s | {', '.join(affects_versions)} |")
    if time_tracking:
        if time_tracking.get("originalEstimate"):
            lines.append(f"| Original Estimate | {time_tracking['originalEstimate']} |")
        if time_tracking.get("remainingEstimate"):
            lines.append(f"| Remaining Estimate | {time_tracking['remainingEstimate']} |")
        if time_tracking.get("timeSpent"):
            lines.append(f"| Time Spent | {time_tracking['timeSpent']} |")
    if isinstance(votes, dict) and votes.get("votes", 0) > 0:
        lines.append(f"| Votes | {votes['votes']} |")
    if isinstance(watches, dict) and watches.get("watchCount", 0) > 0:
        lines.append(f"| Watchers | {watches['watchCount']} |")
    if isinstance(security, dict) and security.get("name"):
        lines.append(f"| Security Level | {security['name']} |")
    lines.append("")

    # Environment
    if environment:
        lines.append("## Environment\n")
        lines.append(environment)
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

    # Work log
    worklog_data = fields.get("worklog", {})
    worklogs = worklog_data.get("worklogs", []) if isinstance(worklog_data, dict) else []
    if worklogs:
        lines.append("## Work Log\n")
        for wl in worklogs:
            wl_author = (wl.get("author") or {}).get("displayName", "Unknown")
            wl_date = (wl.get("started") or "")[:10]
            wl_time = wl.get("timeSpent", "")
            wl_comment = wl.get("comment", "")
            entry = f"- **{wl_author}** ({wl_date}): {wl_time}"
            if wl_comment:
                entry += f" — {wl_comment}"
            lines.append(entry)
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

    # Change history
    history_entries = changelog.get("histories", []) if isinstance(changelog, dict) else []
    if history_entries:
        lines.append("## Change History\n")
        for entry in history_entries:
            ch_author = (entry.get("author") or {}).get("displayName", "Unknown")
            ch_date = (entry.get("created") or "")[:16].replace("T", " ")
            for item in entry.get("items", []):
                field_name = item.get("field", "")
                from_val = item.get("fromString", "") or ""
                to_val = item.get("toString", "") or ""
                lines.append(
                    f"- {ch_date} **{ch_author}** changed **{field_name}**: "
                    f"{from_val} \u2192 {to_val}"
                )
        lines.append("")

    # Remaining custom fields (catch-all)
    custom_lines: list[str] = []
    for fname, fvalue in fields.items():
        if not fname.startswith("customfield_"):
            continue
        if fname in consumed_custom:
            continue
        if fvalue is None:
            continue
        formatted = _format_custom_value(fvalue)
        if formatted:
            custom_lines.append(f"| {fname} | {formatted} |")

    if custom_lines:
        lines.append("## Custom Fields\n")
        lines.append("| Field | Value |")
        lines.append("|---|---|")
        lines.extend(custom_lines)
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

    def _agile_base(self, source) -> str:
        return f"{source.jira_url}/rest/agile/1.0"

    # ---- field discovery ---------------------------------------------------

    async def _fetch_field_mapping(self, source) -> dict[str, str]:
        """Discover custom field IDs for sprint, story points, etc."""
        headers = self._headers(source.jira_token)
        base = self._api_base(source)
        mapping: dict[str, str] = {}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(f"{base}/field", headers=headers)
                if resp.status_code != 200:
                    logger.warning("Could not fetch Jira field list: %s", resp.status_code)
                    return mapping

                for field_def in resp.json():
                    fid = field_def.get("id", "")
                    name = (field_def.get("name") or "").lower()
                    schema = field_def.get("schema", {})
                    custom_type = schema.get("custom", "")

                    if "sprint" in name or "gh-sprint" in custom_type:
                        mapping["sprint"] = fid
                    elif name in ("story points", "story point estimate") or "story-points" in custom_type:
                        mapping["story_points"] = fid
                    elif name == "epic link" and fid.startswith("customfield_"):
                        mapping["epic"] = fid
        except Exception as e:
            logger.warning("Field discovery failed: %s", e)

        logger.info("Jira field mapping: %s", mapping)
        return mapping

    # ---- board & sprint sync -----------------------------------------------

    async def _sync_boards(self, source, local_root: Path) -> int:
        """Sync board and sprint data from the Agile API. Returns file count."""
        headers = self._headers(source.jira_token)
        agile = self._agile_base(source)
        count = 0

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                # List boards for the project
                boards_resp = await client.get(
                    f"{agile}/board",
                    params={"projectKeyOrId": source.jira_project, "maxResults": 50},
                    headers=headers,
                )
                if boards_resp.status_code != 200:
                    logger.warning(
                        "Agile board API unavailable (%s): %s",
                        boards_resp.status_code,
                        boards_resp.text[:200],
                    )
                    return 0

                boards = boards_resp.json().get("values", [])

                for board in boards:
                    board_id = board.get("id")
                    board_name = board.get("name", f"Board-{board_id}")
                    board_type = board.get("type", "unknown")
                    safe_name = _sanitize_filename(board_name)

                    # Fetch sprints for this board
                    sprints: list[dict] = []
                    sprint_start = 0
                    while True:
                        sp_resp = await client.get(
                            f"{agile}/board/{board_id}/sprint",
                            params={"startAt": sprint_start, "maxResults": 50},
                            headers=headers,
                        )
                        if sp_resp.status_code != 200:
                            break  # Kanban boards may not support sprints
                        sp_data = sp_resp.json()
                        batch = sp_data.get("values", [])
                        sprints.extend(batch)
                        if sp_data.get("isLast", True) or not batch:
                            break
                        sprint_start += len(batch)

                    # Write board summary file
                    board_dir = local_root / "boards"
                    board_dir.mkdir(parents=True, exist_ok=True)
                    board_file = board_dir / f"{board_id}-{safe_name}.md"

                    blines = [
                        f"# Board: {board_name}\n",
                        "| Field | Value |",
                        "|---|---|",
                        f"| ID | {board_id} |",
                        f"| Type | {board_type} |",
                        f"| Project | {source.jira_project} |",
                        "",
                    ]

                    if sprints:
                        blines.append("## Sprints\n")
                        blines.append("| Sprint | State | Start | End | Goal |")
                        blines.append("|---|---|---|---|---|")
                        for sp in sprints:
                            sp_name = sp.get("name", "")
                            sp_state = sp.get("state", "")
                            sp_start = (sp.get("startDate") or "")[:10]
                            sp_end = (sp.get("endDate") or "")[:10]
                            sp_goal = (sp.get("goal") or "").replace("|", "/").replace("\n", " ")
                            blines.append(
                                f"| {sp_name} | {sp_state} | {sp_start} | {sp_end} | {sp_goal} |"
                            )
                        blines.append("")

                    board_file.write_text("\n".join(blines), encoding="utf-8")
                    count += 1

                    # Write individual sprint files with issue lists
                    if sprints:
                        sprint_dir = local_root / "sprints"
                        sprint_dir.mkdir(parents=True, exist_ok=True)

                        for sp in sprints:
                            sp_id = sp.get("id")
                            sp_name = sp.get("name", f"Sprint-{sp_id}")
                            sp_state = sp.get("state", "")
                            sp_start = (sp.get("startDate") or "")[:10]
                            sp_end = (sp.get("endDate") or "")[:10]
                            sp_complete = (sp.get("completeDate") or "")[:10]
                            sp_goal = sp.get("goal") or ""
                            safe_sp = _sanitize_filename(sp_name)

                            slines = [
                                f"# Sprint: {sp_name}\n",
                                "| Field | Value |",
                                "|---|---|",
                                f"| ID | {sp_id} |",
                                f"| Board | {board_name} |",
                                f"| State | {sp_state} |",
                                f"| Start Date | {sp_start} |",
                                f"| End Date | {sp_end} |",
                            ]
                            if sp_complete:
                                slines.append(f"| Completed | {sp_complete} |")
                            slines.append("")

                            if sp_goal:
                                slines.append("## Goal\n")
                                slines.append(sp_goal)
                                slines.append("")

                            # Fetch issues in this sprint
                            try:
                                issues_resp = await client.get(
                                    f"{agile}/sprint/{sp_id}/issue",
                                    params={
                                        "maxResults": 200,
                                        "fields": "key,summary,status,assignee,issuetype",
                                    },
                                    headers=headers,
                                )
                                if issues_resp.status_code == 200:
                                    sp_issues = issues_resp.json().get("issues", [])
                                    if sp_issues:
                                        slines.append("## Issues\n")
                                        slines.append(
                                            "| Key | Type | Summary | Status | Assignee |"
                                        )
                                        slines.append("|---|---|---|---|---|")
                                        for si in sp_issues:
                                            si_key = si.get("key", "")
                                            si_f = si.get("fields", {})
                                            si_type = (si_f.get("issuetype") or {}).get("name", "")
                                            si_summ = (si_f.get("summary") or "").replace("|", "/")
                                            si_stat = (si_f.get("status") or {}).get("name", "")
                                            si_asgn = (si_f.get("assignee") or {}).get(
                                                "displayName", "Unassigned"
                                            )
                                            slines.append(
                                                f"| {si_key} | {si_type} | {si_summ} "
                                                f"| {si_stat} | {si_asgn} |"
                                            )
                                        slines.append("")
                            except Exception as e:
                                logger.warning("Failed to fetch sprint %s issues: %s", sp_id, e)

                            sp_file = sprint_dir / f"{sp_id}-{safe_sp}.md"
                            sp_file.write_text("\n".join(slines), encoding="utf-8")
                            count += 1

        except Exception as e:
            logger.warning("Board/sprint sync failed: %s", e)

        logger.info("Synced %d board/sprint files for %s", count, source.jira_project)
        return count

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
                    flds = issue.get("fields", {})
                    issue_type = (flds.get("issuetype") or {}).get("name", "Other")
                    summary = flds.get("summary", f"Issue-{key}")
                    updated = flds.get("updated", "")

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

    async def download_file(
        self,
        source,
        remote_path: str,
        local_path: Path,
        *,
        field_map: dict[str, str] | None = None,
    ) -> None:
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
                    "expand": "renderedFields,changelog",
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

        md = _render_issue_md(issue, field_map=field_map)
        local_path.write_text(md, encoding="utf-8")

    # ---- sync override (timestamp-based change tracking) -------------------

    async def sync(self, source, fs, keep_extensions: set[str] | None = None) -> dict:
        """Custom sync with field discovery and board/sprint data."""
        folder_path = source.folder_path
        local_root = fs._resolve_path(folder_path)
        local_root.mkdir(parents=True, exist_ok=True)

        # Discover custom field IDs (sprint, story points, epic link)
        field_map = await self._fetch_field_mapping(source)

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
                await self.download_file(
                    source, rf.remote_path, local_file, field_map=field_map
                )
                stats["downloaded"] += 1
                logger.info("Downloaded: %s", rf.remote_path)
            except Exception as e:
                logger.error("Failed to download %s: %s", rf.remote_path, e)
                stats["errors"] += 1

        # Mirror-delete issues only (boards/sprints are always refreshed)
        issues_root = local_root / "issues"
        if issues_root.exists():
            for local_file in issues_root.rglob("*"):
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

        # Sync boards and sprints from Agile API
        board_count = await self._sync_boards(source, local_root)
        stats["downloaded"] += board_count

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
