"""Base parser class for document parsing."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ParserResult:
    """Result of parsing a document."""

    content: str  # Markdown content
    metadata: dict = field(default_factory=dict)  # Optional metadata (title, author, etc.)
    success: bool = True
    error: str | None = None

    @classmethod
    def failure(cls, error: str) -> "ParserResult":
        """Create a failure result."""
        return cls(content="", success=False, error=error)


class BaseParser(ABC):
    """Base class for document parsers."""

    # File extensions this parser handles (lowercase, with dot)
    extensions: list[str] = []

    @abstractmethod
    def parse(self, file_path: Path) -> ParserResult:
        """Parse a document and return Markdown content.

        Args:
            file_path: Path to the file to parse.

        Returns:
            ParserResult with Markdown content or error.
        """
        pass

    def can_parse(self, file_path: Path) -> bool:
        """Check if this parser can handle the given file."""
        return file_path.suffix.lower() in self.extensions
