"""Sync connector factory."""

from .azure_devops import AzureDevOpsConnector
from .base import BaseSyncConnector
from .box import BoxConnector
from .confluence import ConfluenceConnector
from .github import GitHubConnector
from .google_drive import GoogleDriveConnector
from .jira import JiraConnector
from .sharepoint import SharePointConnector

_connectors: dict[str, BaseSyncConnector] = {
    "sharepoint": SharePointConnector(),
    "google_drive": GoogleDriveConnector(),
    "github": GitHubConnector(),
    "azure_devops": AzureDevOpsConnector(),
    "jira": JiraConnector(),
    "confluence": ConfluenceConnector(),
    "box": BoxConnector(),
}


def get_connector(source_type: str) -> BaseSyncConnector:
    """Get the appropriate connector instance for a source type."""
    connector = _connectors.get(source_type)
    if not connector:
        raise ValueError(f"Unknown sync source type: {source_type}")
    return connector
