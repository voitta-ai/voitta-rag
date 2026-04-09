"""Filesystem connector — live path mapping, no copy/sync."""

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

from .base import BaseSyncConnector, RemoteFile

logger = logging.getLogger(__name__)


class FilesystemSyncConnector(BaseSyncConnector):
    """Maps a folder to a local directory path.

    Unlike remote connectors, the filesystem connector does not copy files.
    The folder resolves directly to fs_path on disk. This connector exists
    so that source_type="filesystem" is a valid sync source type with
    list_files support for re-indexing.
    """

    async def list_files(self, source) -> list[RemoteFile]:
        """List all files under the mapped filesystem path."""
        fs_path = Path(source.fs_path)
        if not fs_path.is_dir():
            logger.warning("Filesystem source path does not exist: %s", fs_path)
            return []

        result = []
        for file_path in fs_path.rglob("*"):
            if not file_path.is_file() or file_path.name.startswith("."):
                continue
            rel = str(file_path.relative_to(fs_path))
            stat = file_path.stat()
            content_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
            modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
            result.append(RemoteFile(
                remote_path=rel,
                size=stat.st_size,
                modified_at=modified_at,
                content_hash=content_hash,
            ))
        return result

    async def download_file(self, source, remote_path: str, local_path: Path) -> None:
        """No-op — filesystem connector reads in place, no download needed."""
        pass

    async def sync(self, source, fs, keep_extensions: set[str] | None = None) -> dict:
        """No-op sync — filesystem connector uses live path mapping.

        Returns empty stats since no files are copied.
        """
        retval = {"downloaded": 0, "deleted": 0, "skipped": 0, "errors": 0}
        return retval
