"""Base class for remote sync connectors."""

import hashlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class RemoteFile:
    """A file on the remote side."""

    remote_path: str  # Relative path within the remote source
    size: int
    modified_at: str  # ISO 8601
    content_hash: str | None = None


class BaseSyncConnector(ABC):
    """Abstract base for all sync connectors."""

    @abstractmethod
    async def list_files(self, source) -> list[RemoteFile]:
        """List all files on the remote, recursively."""
        ...

    @abstractmethod
    async def download_file(self, source, remote_path: str, local_path: Path) -> None:
        """Download a single file from remote to local_path."""
        ...

    async def sync(self, source, fs) -> dict:
        """Perform a full mirror sync.

        1. List all remote files.
        2. Download new/changed files.
        3. Delete local files not on remote.
        4. Clean up empty directories.
        """
        folder_path = source.folder_path
        local_root = fs._resolve_path(folder_path)
        local_root.mkdir(parents=True, exist_ok=True)

        remote_files = await self.list_files(source)
        remote_paths = set()
        stats = {"downloaded": 0, "deleted": 0, "skipped": 0, "errors": 0}

        # Download new/changed files
        for rf in remote_files:
            remote_paths.add(rf.remote_path)
            local_file = local_root / rf.remote_path

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
                await self.download_file(source, rf.remote_path, local_file)
                stats["downloaded"] += 1
                logger.info("Downloaded: %s", rf.remote_path)
            except Exception as e:
                logger.error("Failed to download %s: %s", rf.remote_path, e)
                stats["errors"] += 1

        # Delete local files not on remote (mirror)
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

        logger.info("Sync complete for %s: %s", folder_path, stats)
        return stats
