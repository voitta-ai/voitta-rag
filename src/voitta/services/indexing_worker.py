"""Background worker for continuous document indexing."""

import asyncio
import logging
import threading

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db.database import get_sync_engine
from ..db.models import FolderIndexStatus
from .indexing import get_indexing_service

logger = logging.getLogger(__name__)


class IndexingWorker:
    """Background worker that continuously polls for pending folders and indexes them."""

    def __init__(self):
        self.settings = get_settings()
        self.poll_interval = self.settings.indexing_poll_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start the indexing worker in a background thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Indexing worker already running")
            return

        self._loop = loop
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info(f"Indexing worker started (poll interval: {self.poll_interval}s)")

    def stop(self) -> None:
        """Stop the indexing worker."""
        if self._thread is None:
            return

        logger.info("Stopping indexing worker...")
        self._stop_event.set()
        self._thread.join(timeout=5.0)
        self._thread = None
        logger.info("Indexing worker stopped")

    def _run(self) -> None:
        """Main worker loop."""
        while not self._stop_event.is_set():
            try:
                self._process_pending_folders()
            except Exception as e:
                logger.exception(f"Error in indexing worker: {e}")

            # Wait for next poll interval or stop signal
            self._stop_event.wait(timeout=self.poll_interval)

    def _process_pending_folders(self) -> None:
        """Find and process all folders with pending status."""
        engine = get_sync_engine()

        with Session(engine) as db:
            # Find all folders with pending status
            result = db.execute(
                select(FolderIndexStatus).where(FolderIndexStatus.status == "pending")
            )
            pending_folders = result.scalars().all()

            if not pending_folders:
                return

            logger.info(f"Found {len(pending_folders)} folders pending indexing")
            indexing_service = get_indexing_service()

            for folder_status in pending_folders:
                if self._stop_event.is_set():
                    break

                folder_path = folder_status.folder_path
                logger.info(f"Starting indexing for folder: {folder_path}")

                # Broadcast indexing started via WebSocket
                self._notify_indexing_status(folder_path, "indexing")

                try:
                    files_indexed, total_chunks, files_skipped = indexing_service.index_folder(
                        folder_path, db, force=False
                    )
                    db.commit()
                    logger.info(
                        f"Completed indexing '{folder_path}': "
                        f"{files_indexed} files, {total_chunks} chunks, {files_skipped} skipped"
                    )

                    self._notify_indexing_complete(folder_path, files_indexed, total_chunks)

                except Exception as e:
                    db.rollback()
                    logger.exception(f"Failed to index folder '{folder_path}': {e}")

                    # Update status to error
                    try:
                        folder_status.status = "error"
                        folder_status.error_message = str(e)
                        db.commit()
                    except Exception:
                        db.rollback()

                    self._notify_indexing_status(folder_path, "error")

    def _notify_indexing_status(self, folder_path: str, status: str) -> None:
        """Send WebSocket notification about indexing status change."""
        try:
            from .watcher import file_watcher

            file_watcher.broadcast_event({
                "type": "index_status",
                "path": folder_path,
                "status": status,
            })
        except Exception as e:
            logger.debug(f"Could not send indexing notification: {e}")

    def _notify_indexing_complete(
        self, folder_path: str, files_indexed: int, total_chunks: int
    ) -> None:
        """Send WebSocket notification about indexing completion."""
        try:
            from .watcher import file_watcher

            file_watcher.broadcast_event({
                "type": "index_complete",
                "path": folder_path,
                "files_indexed": files_indexed,
                "total_chunks": total_chunks,
            })
        except Exception as e:
            logger.debug(f"Could not send indexing notification: {e}")


# Global singleton instance
_indexing_worker: IndexingWorker | None = None


def get_indexing_worker() -> IndexingWorker:
    """Get the global indexing worker instance."""
    global _indexing_worker
    if _indexing_worker is None:
        _indexing_worker = IndexingWorker()
    return _indexing_worker
