"""Filesystem watcher using watchdog."""

import asyncio
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from ..config import get_settings


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

        # Schedule the async notification in the event loop
        asyncio.run_coroutine_threadsafe(self._notify_subscribers(event), self._loop)

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
