"""API routes."""

from fastapi import APIRouter

from . import details, files, folders, index, metadata, pages, raw, settings, sync, websocket

api_router = APIRouter()

# Page routes (HTML)
api_router.include_router(pages.router, tags=["pages"])

# API routes
api_router.include_router(raw.router, prefix="/api/raw", tags=["raw"])
api_router.include_router(files.router, prefix="/api/files", tags=["files"])
api_router.include_router(folders.router, prefix="/api/folders", tags=["folders"])
api_router.include_router(metadata.router, prefix="/api/metadata", tags=["metadata"])
api_router.include_router(settings.router, prefix="/api/settings", tags=["settings"])
api_router.include_router(index.router, prefix="/api/index", tags=["index"])
api_router.include_router(details.router, prefix="/api/details", tags=["details"])
api_router.include_router(sync.router, prefix="/api/sync", tags=["sync"])
api_router.include_router(websocket.router, tags=["websocket"])
