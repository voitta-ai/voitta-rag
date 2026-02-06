#!/usr/bin/env python3
"""Rebuild indexed_files table from Qdrant.

Scrolls all chunks in Qdrant, aggregates per-file stats,
and rebuilds the indexed_files table in SQLite.

Usage:
    python scripts/sync_qdrant_stats.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

from tqdm import tqdm

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels
from sqlalchemy.orm import Session

from voitta.config import get_settings
from voitta.db.database import get_sync_engine
from voitta.db.models import Base, IndexedFile


def scan_qdrant(settings) -> dict[str, dict]:
    """Scroll all Qdrant chunks and aggregate per-file stats.

    Returns:
        Dict mapping file_path to {folder_path, index_folder, chunk_count, indexed_at}
    """
    client = QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)

    file_stats: dict[str, dict] = {}
    offset = None

    print(f"Connecting to Qdrant at {settings.qdrant_host}:{settings.qdrant_port}...")
    print(f"Collection: {settings.qdrant_collection}")

    # Get total point count for progress bar
    info = client.get_collection(settings.qdrant_collection)
    total_points = info.points_count or 0
    print(f"Total points: {total_points}")

    pbar = tqdm(total=total_points, unit=" chunks", desc="Scanning Qdrant")

    while True:
        results, offset = client.scroll(
            collection_name=settings.qdrant_collection,
            limit=1000,
            offset=offset,
            with_payload=["file_path", "folder_path", "index_folder", "indexed_at"],
            with_vectors=False,
        )

        for point in results:
            payload = point.payload
            file_path = payload.get("file_path", "")

            if file_path not in file_stats:
                file_stats[file_path] = {
                    "folder_path": payload.get("folder_path", ""),
                    "index_folder": payload.get("index_folder", ""),
                    "chunk_count": 0,
                    "indexed_at": payload.get("indexed_at"),
                }

            file_stats[file_path]["chunk_count"] += 1

        pbar.update(len(results))

        if offset is None:
            break

    pbar.close()
    total_chunks = sum(s["chunk_count"] for s in file_stats.values())
    print(f"Scan complete: {total_chunks} chunks across {len(file_stats)} files")
    return file_stats


def rebuild_indexed_files(file_stats: dict[str, dict], settings) -> None:
    """Clear and rebuild the indexed_files table."""
    engine = get_sync_engine()
    root = settings.root_path

    with Session(engine) as session:
        # Clear existing data
        deleted = session.query(IndexedFile).delete()
        print(f"Cleared {deleted} existing rows from indexed_files")

        # Insert new rows
        inserted = 0
        for file_path, stats in tqdm(file_stats.items(), desc="Writing to DB", unit=" files"):
            # Try to get file size from filesystem
            abs_path = root / file_path
            try:
                file_size = abs_path.stat().st_size if abs_path.is_file() else 0
            except (OSError, PermissionError):
                file_size = 0

            # Parse indexed_at if available
            indexed_at = None
            if stats["indexed_at"]:
                try:
                    indexed_at = datetime.fromisoformat(stats["indexed_at"])
                except (ValueError, TypeError):
                    indexed_at = datetime.now(timezone.utc)

            session.add(IndexedFile(
                file_path=file_path,
                folder_path=stats["folder_path"],
                index_folder=stats["index_folder"],
                content_hash="rebuild",  # placeholder, runtime will update
                file_size=file_size,
                chunk_count=stats["chunk_count"],
                indexed_at=indexed_at or datetime.now(timezone.utc),
            ))
            inserted += 1

        session.commit()
        print(f"Inserted {inserted} rows into indexed_files")


def main():
    settings = get_settings()
    print(f"Database: {settings.db_path}")
    print(f"Root path: {settings.root_path}")
    print()

    # Ensure tables exist
    engine = get_sync_engine()
    Base.metadata.create_all(bind=engine)

    # Phase 1: scan Qdrant
    file_stats = scan_qdrant(settings)

    if not file_stats:
        print("No data found in Qdrant. Nothing to sync.")
        return

    # Phase 2: rebuild SQLite
    print()
    rebuild_indexed_files(file_stats, settings)

    # Summary
    print()
    total_chunks = sum(s["chunk_count"] for s in file_stats.values())
    folders = set(s["folder_path"] for s in file_stats.values())
    print(f"Summary:")
    print(f"  Files:   {len(file_stats)}")
    print(f"  Chunks:  {total_chunks}")
    print(f"  Folders: {len(folders)}")


if __name__ == "__main__":
    main()
