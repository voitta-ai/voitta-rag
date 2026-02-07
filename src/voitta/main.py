"""FastAPI application entry point."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .api.routes import api_router
from .config import get_settings
from .db.database import init_db
from .mcp_server import mcp, UserHeaderMiddleware
from .services.indexing_worker import get_indexing_worker
from .services.watcher import file_watcher

# Get project root for static files and templates
PROJECT_ROOT = Path(__file__).parent.parent.parent


def setup_logging():
    """Configure file-based logging. Wipes log files on each restart."""
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    log_format = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    formatter = logging.Formatter(log_format)

    # App log — mode='w' wipes on restart
    app_handler = logging.FileHandler(log_dir / "app.log", mode="w", encoding="utf-8")
    app_handler.setFormatter(formatter)
    app_handler.setLevel(logging.DEBUG)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(app_handler)

    # Quiet down noisy loggers
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.WARNING)
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


setup_logging()

# Get MCP app early so we can use its lifespan
settings = get_settings()
mcp_app = mcp.http_app(transport=settings.mcp_transport)
mcp_app.add_middleware(UserHeaderMiddleware)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager - composes MCP and app lifespans."""
    # Initialize database
    init_db()

    # Get the event loop
    loop = asyncio.get_running_loop()

    # Start filesystem watcher
    file_watcher.start(loop)

    # Start indexing worker
    indexing_worker = get_indexing_worker()
    indexing_worker.start(loop)

    # Run MCP lifespan alongside our own
    async with mcp_app.lifespan(app):
        yield

    # Stop indexing worker
    indexing_worker.stop()

    # Stop filesystem watcher
    file_watcher.stop()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
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

    # Mount MCP server at /mcp
    app.mount("/mcp", mcp_app)

    return app


# Create app instance
app = create_app()
