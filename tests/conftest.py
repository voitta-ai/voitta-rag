"""Pytest configuration and fixtures."""

import pytest
from pathlib import Path
import tempfile
import shutil


@pytest.fixture(autouse=True)
def reset_caches():
    """Reset all caches before and after each test."""
    from voitta.config import get_settings
    from voitta.db.database import reset_engines

    get_settings.cache_clear()
    reset_engines()
    yield
    get_settings.cache_clear()
    reset_engines()


@pytest.fixture
def temp_root():
    """Create a temporary root directory for testing."""
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def app(temp_root, monkeypatch):
    """Create test application with temporary directories."""
    # Put database outside the root folder to avoid showing up in listings
    db_dir = tempfile.mkdtemp()
    db_path = Path(db_dir) / "test.db"

    # Set environment variables
    monkeypatch.setenv("VOITTA_ROOT_PATH", str(temp_root))
    monkeypatch.setenv("VOITTA_DB_PATH", str(db_path))
    monkeypatch.setenv("VOITTA_DEBUG", "false")

    # Import after setting env vars (caches are already cleared by autouse fixture)
    from voitta.main import create_app

    app = create_app()
    yield app

    # Cleanup db directory
    shutil.rmtree(db_dir, ignore_errors=True)


@pytest.fixture
def client(app):
    """Create test client."""
    from starlette.testclient import TestClient

    with TestClient(app) as client:
        yield client
