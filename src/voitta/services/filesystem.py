"""Filesystem operations service."""

import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from ..config import get_settings


@dataclass
class FileInfo:
    """Information about a file or folder."""

    name: str
    path: str  # Relative to root
    is_dir: bool
    size: int
    modified_at: datetime
    extension: str | None = None


class FilesystemService:
    """Service for filesystem operations."""

    def __init__(self):
        self.settings = get_settings()
        self.root = self.settings.root_path

    def _resolve_path(self, relative_path: str) -> Path:
        """Resolve a relative path to absolute, ensuring it's within root."""
        if not relative_path or relative_path == "/":
            return self.root

        # Normalize and resolve
        clean_path = relative_path.lstrip("/")
        full_path = (self.root / clean_path).resolve()

        # Security: ensure path is within root
        if not str(full_path).startswith(str(self.root)):
            raise ValueError("Path traversal attempt detected")

        return full_path

    def _to_relative(self, absolute_path: Path) -> str:
        """Convert absolute path to relative (from root)."""
        try:
            return str(absolute_path.relative_to(self.root))
        except ValueError:
            return str(absolute_path)

    def _calculate_dir_size(self, path: Path) -> int:
        """Calculate total size of a directory recursively."""
        total = 0
        try:
            for item in path.rglob("*"):
                if item.is_file():
                    try:
                        total += item.stat().st_size
                    except (PermissionError, OSError):
                        continue
        except (PermissionError, OSError):
            pass
        return total

    def _get_file_info(self, path: Path, calculate_dir_size: bool = True) -> FileInfo:
        """Get file info for a path."""
        stat = path.stat()
        is_dir = path.is_dir()

        if is_dir and calculate_dir_size:
            size = self._calculate_dir_size(path)
        elif is_dir:
            size = 0
        else:
            size = stat.st_size

        return FileInfo(
            name=path.name,
            path=self._to_relative(path),
            is_dir=is_dir,
            size=size,
            modified_at=datetime.fromtimestamp(stat.st_mtime),
            extension=path.suffix.lower() if not is_dir and path.suffix else None,
        )

    def list_directory(self, relative_path: str = "") -> list[FileInfo]:
        """List contents of a directory."""
        dir_path = self._resolve_path(relative_path)

        if not dir_path.exists():
            raise FileNotFoundError(f"Directory not found: {relative_path}")

        if not dir_path.is_dir():
            raise NotADirectoryError(f"Not a directory: {relative_path}")

        items = []
        for item in sorted(dir_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            # Skip hidden files
            if item.name.startswith("."):
                continue
            try:
                items.append(self._get_file_info(item))
            except (PermissionError, OSError):
                continue

        return items

    def get_info(self, relative_path: str) -> FileInfo:
        """Get info for a specific file or folder."""
        path = self._resolve_path(relative_path)

        if not path.exists():
            raise FileNotFoundError(f"Path not found: {relative_path}")

        return self._get_file_info(path)

    def create_folder(self, relative_path: str, name: str) -> FileInfo:
        """Create a new folder."""
        parent = self._resolve_path(relative_path)

        if not parent.exists():
            raise FileNotFoundError(f"Parent directory not found: {relative_path}")

        # Sanitize folder name
        safe_name = "".join(c for c in name if c.isalnum() or c in "._- ")
        if not safe_name:
            raise ValueError("Invalid folder name")

        new_folder = parent / safe_name

        if new_folder.exists():
            raise FileExistsError(f"Folder already exists: {safe_name}")

        new_folder.mkdir(parents=False)
        return self._get_file_info(new_folder)

    def upload_file(self, relative_path: str, filename: str, file: BinaryIO) -> FileInfo:
        """Upload a file to a directory."""
        parent = self._resolve_path(relative_path)

        if not parent.exists():
            raise FileNotFoundError(f"Directory not found: {relative_path}")

        if not parent.is_dir():
            raise NotADirectoryError(f"Not a directory: {relative_path}")

        # Sanitize filename
        safe_name = "".join(c for c in filename if c.isalnum() or c in "._- ")
        if not safe_name:
            raise ValueError("Invalid filename")

        file_path = parent / safe_name

        # Write file
        with open(file_path, "wb") as f:
            shutil.copyfileobj(file, f)

        return self._get_file_info(file_path)

    def exists(self, relative_path: str) -> bool:
        """Check if a path exists."""
        try:
            path = self._resolve_path(relative_path)
            return path.exists()
        except ValueError:
            return False

    def is_dir(self, relative_path: str) -> bool:
        """Check if path is a directory."""
        try:
            path = self._resolve_path(relative_path)
            return path.is_dir()
        except ValueError:
            return False

    def get_breadcrumbs(self, relative_path: str) -> list[tuple[str, str]]:
        """Get breadcrumb navigation for a path."""
        if not relative_path or relative_path == "/":
            return [("Root", "")]

        breadcrumbs = [("Root", "")]
        parts = relative_path.strip("/").split("/")
        current = ""

        for part in parts:
            current = f"{current}/{part}" if current else part
            breadcrumbs.append((part, current))

        return breadcrumbs

    def count_files_recursive(self, relative_path: str) -> int:
        """Count all files recursively within a folder."""
        dir_path = self._resolve_path(relative_path)

        if not dir_path.exists() or not dir_path.is_dir():
            return 0

        count = 0
        try:
            for item in dir_path.rglob("*"):
                if item.is_file() and not item.name.startswith("."):
                    count += 1
        except (PermissionError, OSError):
            pass

        return count
