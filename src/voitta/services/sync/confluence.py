"""Confluence sync connector - Pages from Confluence Server/Data Center."""

import hashlib
import json
import logging
import re
from pathlib import Path

import httpx

from .base import BaseSyncConnector, RemoteFile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sanitize_filename(name: str) -> str:
    """Sanitize a string for use as a filename/path component."""
    sanitized = re.sub(r'[<>:"/\\|?*]', "-", name)
    sanitized = re.sub(r"\s+", "-", sanitized)
    sanitized = re.sub(r"-{2,}", "-", sanitized)
    return sanitized.strip("-")[:100]


def _html_to_markdown(html: str) -> str:
    """Convert Confluence storage format HTML to markdown."""
    if not html:
        return ""

    text = html

    # Headers
    for i in range(6, 0, -1):
        text = re.sub(rf"<h{i}[^>]*>(.*?)</h{i}>", rf"{'#' * i} \1\n", text, flags=re.DOTALL)

    # Line breaks and paragraphs
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</?p[^>]*>", "\n", text)

    # Bold and italic
    text = re.sub(r"<strong[^>]*>(.*?)</strong>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<b[^>]*>(.*?)</b>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<em[^>]*>(.*?)</em>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<i[^>]*>(.*?)</i>", r"*\1*", text, flags=re.DOTALL)

    # Links
    text = re.sub(r'<a[^>]+href="([^"]*)"[^>]*>(.*?)</a>', r"[\2](\1)", text, flags=re.DOTALL)

    # Lists
    text = re.sub(r"<li[^>]*>(.*?)</li>", r"- \1\n", text, flags=re.DOTALL)
    text = re.sub(r"</?[ou]l[^>]*>", "\n", text)

    # Code blocks (Confluence uses ac:structured-macro for code)
    text = re.sub(
        r'<ac:structured-macro[^>]*ac:name="code"[^>]*>.*?<ac:plain-text-body><!\[CDATA\[(.*?)\]\]></ac:plain-text-body>.*?</ac:structured-macro>',
        r"```\n\1\n```\n",
        text,
        flags=re.DOTALL
    )

    # Inline code
    text = re.sub(r"<code[^>]*>(.*?)</code>", r"`\1`", text, flags=re.DOTALL)

    # Tables - simplified conversion
    text = re.sub(r"<table[^>]*>", "\n", text)
    text = re.sub(r"</table>", "\n", text)
    text = re.sub(r"<tr[^>]*>", "", text)
    text = re.sub(r"</tr>", " |\n", text)
    text = re.sub(r"<t[hd][^>]*>(.*?)</t[hd]>", r"| \1 ", text, flags=re.DOTALL)

    # Divs and spans
    text = re.sub(r"</?div[^>]*>", "\n", text)
    text = re.sub(r"</?span[^>]*>", "", text)

    # Confluence macros - extract content or remove
    text = re.sub(r"<ac:structured-macro[^>]*>.*?</ac:structured-macro>", "", text, flags=re.DOTALL)
    text = re.sub(r"<ac:[^>]+/>", "", text)
    text = re.sub(r"<ac:[^>]+>.*?</ac:[^>]+>", "", text, flags=re.DOTALL)
    text = re.sub(r"<ri:[^>]+/>", "", text)

    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)

    # Clean up whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)

    # Decode HTML entities
    text = text.replace("&nbsp;", " ")
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&quot;", '"')

    return text.strip()


def _render_page_md(page: dict, attachments: list) -> str:
    """Render a Confluence page as a structured markdown document."""
    page_id = page["id"]
    title = page.get("title", f"Page {page_id}")
    space = page.get("space", {})
    space_key = space.get("key", "")
    space_name = space.get("name", "")

    version = page.get("version", {})
    version_num = version.get("number", 1)
    last_updated = version.get("when", "")[:10] if version.get("when") else ""
    updated_by = version.get("by", {}).get("displayName", "Unknown")

    created_by = page.get("history", {}).get("createdBy", {}).get("displayName", "Unknown")
    created_date = page.get("history", {}).get("createdDate", "")[:10] if page.get("history", {}).get("createdDate") else ""

    # Labels
    labels = [label.get("name", "") for label in page.get("metadata", {}).get("labels", {}).get("results", [])]

    # Body content
    body_content = page.get("body", {}).get("storage", {}).get("value", "")

    lines: list[str] = []
    lines.append(f"# {title}\n")

    # Metadata table
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(f"| Space | {space_name} ({space_key}) |")
    lines.append(f"| Created By | {created_by} |")
    if created_date:
        lines.append(f"| Created | {created_date} |")
    lines.append(f"| Updated By | {updated_by} |")
    if last_updated:
        lines.append(f"| Updated | {last_updated} |")
    lines.append(f"| Version | {version_num} |")
    if labels:
        lines.append(f"| Labels | {', '.join(labels)} |")
    lines.append("")

    # Content
    if body_content:
        lines.append("## Content\n")
        lines.append(_html_to_markdown(body_content))
        lines.append("")

    # Attachments
    if attachments:
        lines.append("## Attachments\n")
        for att in attachments:
            att_title = att.get("title", "attachment")
            download_link = att.get("_links", {}).get("download", "")
            if download_link and not download_link.startswith("http"):
                # Make absolute URL
                download_link = f"{page.get('_base_url', '')}{download_link}"
            size = att.get("extensions", {}).get("fileSize", 0)
            size_kb = size // 1024 if size else 0
            lines.append(f"- [{att_title}]({download_link}) ({size_kb} KB)")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class ConfluenceConnector(BaseSyncConnector):

    def _headers(self, token: str) -> dict:
        """Build auth headers for Confluence Server/DC with PAT."""
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _api_base(self, source) -> str:
        return f"{source.confluence_url}/rest/api"

    async def _get_all_pages_in_space(self, client, headers, base_url, space_key) -> list[dict]:
        """Get all pages in a space with pagination."""
        pages = []
        start = 0
        limit = 50

        while True:
            resp = await client.get(
                f"{base_url}/content",
                params={
                    "spaceKey": space_key,
                    "type": "page",
                    "start": start,
                    "limit": limit,
                    "expand": "ancestors,version,history",
                },
                headers=headers,
            )

            if resp.status_code == 401:
                raise RuntimeError("Confluence authentication failed. Check your token.")
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Confluence API failed ({resp.status_code}): {resp.text[:500]}"
                )

            data = resp.json()
            results = data.get("results", [])
            pages.extend(results)

            # Check for more pages
            if len(results) < limit:
                break
            start += limit

        return pages

    def _build_page_path(self, page: dict, page_map: dict[str, dict]) -> str:
        """Build hierarchical path for a page based on ancestors."""
        ancestors = page.get("ancestors", [])
        path_parts = []

        for ancestor in ancestors:
            ancestor_title = ancestor.get("title", "")
            if ancestor_title:
                path_parts.append(_sanitize_filename(ancestor_title))

        # Add current page with ID prefix for reliable lookup
        title = page.get("title", f"Page-{page['id']}")
        page_id = page["id"]
        path_parts.append(f"{page_id}-{_sanitize_filename(title)}")

        return "/".join(path_parts) + ".md"

    # ---- list_files -------------------------------------------------------

    async def list_files(self, source) -> list[RemoteFile]:
        if not source.confluence_token:
            raise RuntimeError("Confluence token not configured")
        if not source.confluence_space:
            raise RuntimeError("Confluence space not configured")

        files: list[RemoteFile] = []
        headers = self._headers(source.confluence_token)
        base = self._api_base(source)

        async with httpx.AsyncClient(timeout=60.0) as client:
            pages = await self._get_all_pages_in_space(
                client, headers, base, source.confluence_space
            )

            # Build map for path resolution
            page_map = {p["id"]: p for p in pages}

            for page in pages:
                page_id = page["id"]
                version = page.get("version", {}).get("number", 1)
                updated = page.get("version", {}).get("when", "")

                remote_path = f"pages/{self._build_page_path(page, page_map)}"

                # Use version number as content hash for change detection
                content_hash = hashlib.sha256(f"{version}:{updated}".encode()).hexdigest()

                created_date = page.get("history", {}).get("createdDate", "")

                files.append(
                    RemoteFile(
                        remote_path=remote_path,
                        size=0,
                        modified_at=updated,
                        content_hash=content_hash,
                        created_at=created_date,
                    )
                )

        logger.info(
            "Listed %d pages from Confluence space %s",
            len(files), source.confluence_space
        )
        return files

    # ---- download_file ----------------------------------------------------

    async def download_file(self, source, remote_path: str, local_path: Path) -> None:
        if not source.confluence_token:
            raise RuntimeError("Confluence token not configured")

        # Extract page ID from path: pages/.../1553249817-Title.md
        stem = Path(remote_path).stem  # e.g. "1553249817-DQ-Dashboard-WIP"
        m = re.match(r"^(\d+)-", stem)
        if not m:
            raise RuntimeError(f"Cannot parse page ID from path: {remote_path}")
        page_id = m.group(1)

        headers = self._headers(source.confluence_token)
        base = self._api_base(source)

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Fetch page directly by ID
            resp = await client.get(
                f"{base}/content/{page_id}",
                params={
                    "expand": "body.storage,version,space,history,metadata.labels,ancestors",
                },
                headers=headers,
            )

            if resp.status_code == 401:
                raise RuntimeError("Confluence authentication failed. Check your token.")
            if resp.status_code == 404:
                raise RuntimeError(f"Page not found: {page_id}")
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Failed to fetch page {page_id}: {resp.text[:300]}"
                )

            page = resp.json()

            # Get attachments
            page_id = page["id"]
            resp = await client.get(
                f"{base}/content/{page_id}/child/attachment",
                params={"limit": 100},
                headers=headers,
            )

            attachments = []
            if resp.status_code == 200:
                attachments = resp.json().get("results", [])

            # Add base URL for attachment links
            page["_base_url"] = source.confluence_url

        md = _render_page_md(page, attachments)
        local_path.write_text(md, encoding="utf-8")

    # ---- sync override (version-based change tracking) -------------------

    async def sync(self, source, fs, keep_extensions: set[str] | None = None) -> dict:
        """Custom sync using version-based change detection via sidecar file."""
        folder_path = source.folder_path
        local_root = fs._resolve_path(folder_path)
        local_root.mkdir(parents=True, exist_ok=True)

        hash_file = local_root / ".confluence_revisions.json"
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

        # Write timestamps sidecar for the indexing pipeline
        timestamps = {}
        for rf in remote_files:
            entry = {}
            if rf.modified_at:
                entry["modified_at"] = rf.modified_at
            if rf.created_at:
                entry["created_at"] = rf.created_at
            if entry:
                timestamps[rf.remote_path] = entry
        (local_root / ".voitta_timestamps.json").write_text(json.dumps(timestamps))

        hash_file.write_text(json.dumps(new_revisions), encoding="utf-8")
        logger.info("Sync complete for %s: %s", folder_path, stats)
        return stats
