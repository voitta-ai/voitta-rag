"""FastAPI application entry point."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .api.routes import api_router
from .config import get_settings
from .db.database import init_db
from .services.watcher import file_watcher

# Get project root for static files and templates
PROJECT_ROOT = Path(__file__).parent.parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Initialize database
    init_db()

    # Start filesystem watcher
    loop = asyncio.get_running_loop()
    file_watcher.start(loop)

    yield

    # Stop filesystem watcher
    file_watcher.stop()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="voitta-rag",
        description="Web-based file management system",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Mount static files
    static_path = PROJECT_ROOT / "static"
    if static_path.exists():
        app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

    # Set up Jinja2 templates
    templates_path = Path(__file__).parent / "web" / "templates"
    app.state.templates = Jinja2Templates(directory=str(templates_path))

    # Include routes
    app.include_router(api_router)

    return app


# Create app instance
app = create_app()
