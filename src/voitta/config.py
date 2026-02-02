"""Application configuration."""

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


class Settings:
    """Application settings loaded from environment variables."""

    def __init__(self):
        # Core settings
        self.root_path: Path = Path(
            os.getenv("VOITTA_ROOT_PATH", "./data")
        ).resolve()
        self.db_path: Path = Path(
            os.getenv("VOITTA_DB_PATH", "./voitta.db")
        ).resolve()
        self.host: str = os.getenv("VOITTA_HOST", "0.0.0.0")
        self.port: int = int(os.getenv("VOITTA_PORT", "8000"))
        self.debug: bool = os.getenv("VOITTA_DEBUG", "false").lower() == "true"

        # Qdrant settings
        self.qdrant_host: str = os.getenv("QDRANT_HOST", "localhost")
        self.qdrant_port: int = int(os.getenv("QDRANT_PORT", "6333"))
        self.qdrant_collection: str = os.getenv("QDRANT_COLLECTION", "voitta_documents")

        # Embedding settings
        self.embedding_model: str = os.getenv("EMBEDDING_MODEL", "intfloat/e5-base-v2")
        self.embedding_dimension: int = int(os.getenv("EMBEDDING_DIMENSION", "768"))

        # Chunking settings
        self.chunk_size: int = int(os.getenv("CHUNK_SIZE", "512"))
        self.chunk_overlap: int = int(os.getenv("CHUNK_OVERLAP", "50"))
        self.chunking_strategy: str = os.getenv("CHUNKING_STRATEGY", "recursive")

        # Indexing worker settings
        self.indexing_poll_interval: int = int(os.getenv("INDEXING_POLL_INTERVAL", "10"))

        # MCP server settings
        self.mcp_port: int = int(os.getenv("MCP_PORT", "8001"))
        self.mcp_transport: str = os.getenv("MCP_TRANSPORT", "streamable-http")  # streamable-http or sse
        self.mcp_search_limit: int = int(os.getenv("MCP_SEARCH_LIMIT", "20"))

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

    @property
    def qdrant_url(self) -> str:
        """Qdrant connection URL."""
        return f"http://{self.qdrant_host}:{self.qdrant_port}"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
