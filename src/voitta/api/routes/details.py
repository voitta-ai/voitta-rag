"""Item details API for sidebar."""

from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from ..deps import DB, CurrentUser, Filesystem, Metadata
from ...db.models import FolderIndexStatus, IndexedFile, UserFolderSetting
from ...services.vector_store import get_vector_store

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

    info = fs.get_info(path)
    is_dir = info.is_dir

    # Get metadata
    metadata_text, metadata_updated_by = await metadata_svc.get_metadata_with_user(path)

    # Get folder-specific data
    folder_enabled = None
    search_active = None
    index_status = None
    chunk_count = None
    indexed_at = None

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
        search_active = setting.search_active if setting else False

        # Get index status
        result = await db.execute(
            select(FolderIndexStatus).where(FolderIndexStatus.folder_path == path)
        )
        status_row = result.scalar_one_or_none()
        index_status = status_row.status if status_row else "none"

        # Calculate file type stats (recursive)
        file_type_stats = await _get_file_type_stats(fs, db, path)
    else:
        # Get file index info from Qdrant (real-time chunk count)
        vector_store = get_vector_store()
        chunk_count = vector_store.count_by_file(path)
        if chunk_count > 0:
            index_status = "indexed"
            indexed_at = None  # Not available from Qdrant directly
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
    )


async def _get_file_type_stats(fs: Filesystem, db: DB, folder_path: str) -> list[FileTypeStat]:
    """Get file type statistics for a folder (recursive)."""
    # Get the absolute path to the folder
    root = fs.root
    abs_folder_path = root / folder_path if folder_path else root

    if not abs_folder_path.exists() or not abs_folder_path.is_dir():
        return []

    # Count files by extension from filesystem
    ext_counts: dict[str, int] = defaultdict(int)

    for item in abs_folder_path.rglob("*"):
        if item.is_file() and not item.name.startswith("."):
            ext = item.suffix.lower() if item.suffix else "(no extension)"
            ext_counts[ext] += 1

    if not ext_counts:
        return []

    # Get chunk counts from Qdrant (real-time)
    vector_store = get_vector_store()
    prefix = folder_path + "/" if folder_path else ""
    qdrant_file_counts = vector_store.get_file_chunk_counts(prefix)

    # Group by extension
    indexed_by_ext: dict[str, tuple[int, int]] = defaultdict(lambda: (0, 0))
    for file_path, chunks in qdrant_file_counts.items():
        ext = Path(file_path).suffix.lower() if Path(file_path).suffix else "(no extension)"
        count, total_chunks = indexed_by_ext[ext]
        indexed_by_ext[ext] = (count + 1, total_chunks + chunks)

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
