"""Item details API for sidebar."""

import os
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select

from ..deps import DB, CurrentUser, Filesystem, Metadata, get_active_project
from ...db.models import FolderIndexStatus, FolderSyncSource, IndexedFile, ProjectFolderSetting, UserFolderSetting

router = APIRouter()


class FileTypeStat(BaseModel):
    """Stats for a single file type."""

    extension: str
    total_count: int
    indexed_count: int
    chunk_count: int


class ItemDetailsResponse(BaseModel):
    """Response with all details for sidebar display."""

    path: str
    name: str
    is_dir: bool
    # Metadata
    metadata_text: str | None = None
    metadata_updated_by: str | None = None
    # Folder-specific (only if is_dir)
    folder_enabled: bool | None = None
    search_active: bool | None = None  # For MCP search filtering
    index_status: str | None = None
    file_type_stats: list[FileTypeStat] | None = None
    # File-specific index info (only if not is_dir)
    chunk_count: int | None = None
    indexed_at: str | None = None
    # Sync source info (folder-only)
    sync_source_type: str | None = None
    sync_status: str | None = None
    last_synced_at: str | None = None
    is_empty: bool | None = None


@router.get("/{path:path}")
async def get_item_details(
    path: str,
    user: CurrentUser,
    fs: Filesystem,
    metadata_svc: Metadata,
    db: DB,
):
    """Get all details for an item (file or folder)."""
    if not fs.exists(path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Path not found: {path}",
        )

    info = fs.get_info(path, calculate_dir_size=False)
    is_dir = info.is_dir

    # Get metadata
    metadata_text, metadata_updated_by = await metadata_svc.get_metadata_with_user(path)

    # Get folder-specific data
    folder_enabled = None
    search_active = None
    index_status = None
    chunk_count = None
    indexed_at = None
    sync_source_type = None
    sync_status = None
    last_synced_at = None
    is_empty = None

    file_type_stats = None

    if is_dir:
        # Get folder enabled setting for this user
        result = await db.execute(
            select(UserFolderSetting).where(
                UserFolderSetting.user_id == user.id,
                UserFolderSetting.folder_path == path,
            )
        )
        setting = result.scalar_one_or_none()
        folder_enabled = setting.enabled if setting else False

        # Get search_active from active project
        project = await get_active_project(user, db)
        if project.is_default:
            search_active = setting.search_active if setting else False
        else:
            result = await db.execute(
                select(ProjectFolderSetting).where(
                    ProjectFolderSetting.project_id == project.id,
                    ProjectFolderSetting.folder_path == path,
                )
            )
            project_setting = result.scalar_one_or_none()
            search_active = project_setting.search_active if project_setting else False

        # Get index status
        result = await db.execute(
            select(FolderIndexStatus).where(FolderIndexStatus.folder_path == path)
        )
        status_row = result.scalar_one_or_none()
        index_status = status_row.status if status_row else "none"

        # Calculate file type stats from indexed_files + filesystem
        file_type_stats = await _get_file_type_stats(fs, db, path)

        # Check for sync source
        sync_result = await db.execute(
            select(FolderSyncSource).where(FolderSyncSource.folder_path == path)
        )
        sync_source = sync_result.scalar_one_or_none()
        sync_source_type = sync_source.source_type if sync_source else None
        sync_status = sync_source.sync_status if sync_source else None
        last_synced_at = sync_source.last_synced_at.isoformat() if sync_source and sync_source.last_synced_at else None
        is_empty = fs.is_dir_empty(path)
    else:
        # Get file index info from indexed_files table
        result = await db.execute(
            select(IndexedFile).where(IndexedFile.file_path == path)
        )
        indexed_file = result.scalar_one_or_none()
        if indexed_file and indexed_file.chunk_count != 0:
            index_status = "indexing" if indexed_file.chunk_count < 0 else "indexed"
            chunk_count = abs(indexed_file.chunk_count)
            indexed_at = indexed_file.indexed_at.isoformat() if indexed_file.indexed_at else None
        else:
            index_status = "none"

    return ItemDetailsResponse(
        path=path,
        name=info.name,
        is_dir=is_dir,
        metadata_text=metadata_text,
        metadata_updated_by=metadata_updated_by,
        folder_enabled=folder_enabled,
        search_active=search_active,
        index_status=index_status,
        file_type_stats=file_type_stats,
        chunk_count=chunk_count,
        indexed_at=indexed_at,
        sync_source_type=sync_source_type,
        sync_status=sync_status,
        last_synced_at=last_synced_at,
        is_empty=is_empty,
    )


async def _get_file_type_stats(fs: Filesystem, db: DB, folder_path: str) -> list[FileTypeStat]:
    """Get file type statistics for a folder (recursive)."""
    # Get the absolute path to the folder
    root = fs.root
    abs_folder_path = root / folder_path if folder_path else root

    if not abs_folder_path.exists() or not abs_folder_path.is_dir():
        return []

    # Count files by extension using os.walk (avoids per-file stat calls
    # that pathlib.rglob + is_file() would make)
    ext_counts: dict[str, int] = defaultdict(int)
    abs_str = str(abs_folder_path)

    for dirpath, dirnames, filenames in os.walk(abs_str):
        # Skip hidden directories in-place (prevents descent)
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fname in filenames:
            if fname.startswith("."):
                continue
            _dot = fname.rfind(".")
            ext = fname[_dot:].lower() if _dot > 0 else "(no extension)"
            ext_counts[ext] += 1

    if not ext_counts:
        return []

    # Get indexed file stats from DB instead of Qdrant
    prefix = folder_path + "/" if folder_path else ""
    result = await db.execute(
        select(IndexedFile.file_path, IndexedFile.chunk_count).where(
            IndexedFile.file_path.like(prefix + "%")
        )
    )

    # Group by extension
    indexed_by_ext: dict[str, tuple[int, int]] = defaultdict(lambda: (0, 0))
    for file_path, chunks in result.all():
        ext = Path(file_path).suffix.lower() if Path(file_path).suffix else "(no extension)"
        count, total_chunks = indexed_by_ext[ext]
        indexed_by_ext[ext] = (count + 1, total_chunks + abs(chunks))

    # Build stats per extension
    stats = []
    for ext, total_count in ext_counts.items():
        indexed_count, chunk_count = indexed_by_ext.get(ext, (0, 0))
        stats.append(
            FileTypeStat(
                extension=ext,
                total_count=total_count,
                indexed_count=indexed_count,
                chunk_count=chunk_count,
            )
        )

    # Sort by total count descending
    stats.sort(key=lambda s: s.total_count, reverse=True)

    return stats
