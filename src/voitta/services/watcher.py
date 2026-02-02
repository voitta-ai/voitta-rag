"""Filesystem watcher using watchdog."""

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from ..config import get_settings
from ..db.database import get_sync_engine

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    """File system event types."""

    CREATED = "created"
    DELETED = "deleted"
    MODIFIED = "modified"
    MOVED = "moved"


@dataclass
class FileEvent:
    """File system event data."""

    event_type: EventType
    path: str  # Relative to root
    is_dir: bool
    dest_path: str | None = None  # For move events


class FileWatcherHandler(FileSystemEventHandler):
    """Handler for filesystem events."""

    def __init__(self, root: Path, callback: Callable[[FileEvent], None]):
        self.root = root
        self.callback = callback

    def _to_relative(self, path: str) -> str:
        """Convert absolute path to relative."""
        try:
            return str(Path(path).relative_to(self.root))
        except ValueError:
            return path

    def _create_event(
        self, event_type: EventType, event: FileSystemEvent, dest_path: str | None = None
    ) -> FileEvent:
        return FileEvent(
            event_type=event_type,
            path=self._to_relative(event.src_path),
            is_dir=event.is_directory,
            dest_path=self._to_relative(dest_path) if dest_path else None,
        )

    def on_created(self, event: FileSystemEvent):
        # Skip hidden files
        if Path(event.src_path).name.startswith("."):
            return
        self.callback(self._create_event(EventType.CREATED, event))

    def on_deleted(self, event: FileSystemEvent):
        if Path(event.src_path).name.startswith("."):
            return
        self.callback(self._create_event(EventType.DELETED, event))

    def on_modified(self, event: FileSystemEvent):
        if Path(event.src_path).name.startswith("."):
            return
        # Skip directory modification events (too noisy)
        if event.is_directory:
            return
        self.callback(self._create_event(EventType.MODIFIED, event))

    def on_moved(self, event: FileSystemEvent):
        if Path(event.src_path).name.startswith("."):
            return
        self.callback(
            self._create_event(EventType.MOVED, event, dest_path=event.dest_path)
        )


class FileWatcher:
    """Filesystem watcher that pushes events to connected clients."""

    def __init__(self):
        self.settings = get_settings()
        self.root = self.settings.root_path
        self.observer: Observer | None = None
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def _on_event(self, event: FileEvent):
        """Handle filesystem event and notify subscribers."""
        if self._loop is None:
            return

        # Handle file/folder deletions - remove associated chunks from vector store
        if event.event_type == EventType.DELETED:
            self._handle_deletion(event)

        # Schedule the async notification in the event loop
        asyncio.run_coroutine_threadsafe(self._notify_subscribers(event), self._loop)

    def _handle_deletion(self, event: FileEvent) -> None:
        """Handle file or folder deletion by removing associated chunks."""
        try:
            from .indexing import get_indexing_service

            engine = get_sync_engine()
            indexing_service = get_indexing_service()

            with Session(engine) as db:
                if event.is_dir:
                    # Remove all file indexes for the deleted folder
                    count = indexing_service.remove_folder_index(event.path, db)
                    if count > 0:
                        logger.info(f"Removed index for {count} files in deleted folder: {event.path}")
                else:
                    # Remove single file index
                    removed = indexing_service.remove_file_index(event.path, db)
                    if removed:
                        logger.info(f"Removed index for deleted file: {event.path}")

                db.commit()
        except Exception as e:
            logger.error(f"Error handling deletion for {event.path}: {e}")

    async def _notify_subscribers(self, event: FileEvent):
        """Notify all subscribers of an event."""
        dead_queues = set()
        for queue in self._subscribers:
            try:
                await asyncio.wait_for(queue.put(event), timeout=1.0)
            except asyncio.TimeoutError:
                dead_queues.add(queue)
            except Exception:
                dead_queues.add(queue)

        # Clean up dead queues
        self._subscribers -= dead_queues

    async def _broadcast(self, data: dict[str, Any]) -> None:
        """Broadcast a dictionary event to all subscribers."""
        dead_queues = set()
        for queue in self._subscribers:
            try:
                await asyncio.wait_for(queue.put(data), timeout=1.0)
            except asyncio.TimeoutError:
                dead_queues.add(queue)
            except Exception:
                dead_queues.add(queue)

        # Clean up dead queues
        self._subscribers -= dead_queues

    def subscribe(self) -> asyncio.Queue:
        """Subscribe to filesystem events. Returns a queue to receive events."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue):
        """Unsubscribe from filesystem events."""
        self._subscribers.discard(queue)

    def start(self, loop: asyncio.AbstractEventLoop):
        """Start watching the filesystem."""
        if self.observer is not None:
            return

        self._loop = loop
        handler = FileWatcherHandler(self.root, self._on_event)
        self.observer = Observer()
        self.observer.schedule(handler, str(self.root), recursive=True)
        self.observer.start()

    def stop(self):
        """Stop watching the filesystem."""
        if self.observer is not None:
            self.observer.stop()
            self.observer.join(timeout=5)
            self.observer = None
            self._loop = None


# Global watcher instance
file_watcher = FileWatcher()
