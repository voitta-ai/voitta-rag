"""WebSocket routes for real-time updates."""

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ...services.watcher import FileEvent, file_watcher

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time filesystem events."""
    await websocket.accept()

    # Subscribe to filesystem events
    queue = file_watcher.subscribe()

    try:
        while True:
            try:
                # Wait for an event with timeout to allow checking connection
                event = await asyncio.wait_for(queue.get(), timeout=30.0)

                # Handle both FileEvent objects and plain dict events
                if isinstance(event, dict):
                    await websocket.send_json(event)
                elif isinstance(event, FileEvent):
                    await websocket.send_json(
                        {
                            "type": event.event_type.value,
                            "path": event.path,
                            "is_dir": event.is_dir,
                            "dest_path": event.dest_path,
                        }
                    )
            except asyncio.TimeoutError:
                # Send ping to keep connection alive
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        file_watcher.unsubscribe(queue)
