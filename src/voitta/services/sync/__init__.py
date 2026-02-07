"""Sync connector factory."""

from .base import BaseSyncConnector
from .github import GitHubConnector
from .google_drive import GoogleDriveConnector
from .sharepoint import SharePointConnector

_connectors: dict[str, BaseSyncConnector] = {
    "sharepoint": SharePointConnector(),
    "google_drive": GoogleDriveConnector(),
    "github": GitHubConnector(),
}


def get_connector(source_type: str) -> BaseSyncConnector:
    """Get the appropriate connector instance for a source type."""
    connector = _connectors.get(source_type)
    if not connector:
        raise ValueError(f"Unknown sync source type: {source_type}")
    return connector
