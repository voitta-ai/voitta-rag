"""Application configuration."""

import os
from pathlib import Path
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


class Settings:
    """Application settings loaded from environment variables."""

    def __init__(self):
        self.root_path: Path = Path(
            os.getenv("VOITTA_ROOT_PATH", "./data")
        ).resolve()
        self.db_path: Path = Path(
            os.getenv("VOITTA_DB_PATH", "./voitta.db")
        ).resolve()
        self.host: str = os.getenv("VOITTA_HOST", "0.0.0.0")
        self.port: int = int(os.getenv("VOITTA_PORT", "8000"))
        self.debug: bool = os.getenv("VOITTA_DEBUG", "false").lower() == "true"

        # Ensure root path exists
        self.root_path.mkdir(parents=True, exist_ok=True)

    @property
    def database_url(self) -> str:
        """SQLite database URL."""
        return f"sqlite+aiosqlite:///{self.db_path}"

    @property
    def sync_database_url(self) -> str:
        """Synchronous SQLite database URL."""
        return f"sqlite:///{self.db_path}"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
