"""Parser registry for automatic format detection and parsing."""

from pathlib import Path

from .base import BaseParser, ParserResult
from .docx_parser import DocxParser
from .epub_parser import EpubParser
from .pdf_parser import PdfParser
from .pptx_parser import PptxParser
from .text_parser import TextParser
from .xlsx_parser import XlsxParser
from .odf_parser import OdpParser, OdtParser, OdsParser


class ParserRegistry:
    """Registry of document parsers with automatic format detection."""

    def __init__(self):
        self._parsers: list[BaseParser] = []
        self._extension_map: dict[str, BaseParser] = {}

    def register(self, parser: BaseParser) -> None:
        """Register a parser."""
        self._parsers.append(parser)
        for ext in parser.extensions:
            self._extension_map[ext.lower()] = parser

    def get_parser(self, file_path: Path | str) -> BaseParser | None:
        """Get a parser for the given file based on extension."""
        if isinstance(file_path, str):
            file_path = Path(file_path)

        ext = file_path.suffix.lower()
        return self._extension_map.get(ext)

    def can_parse(self, file_path: Path | str) -> bool:
        """Check if any parser can handle this file."""
        return self.get_parser(file_path) is not None

    def parse(self, file_path: Path | str) -> ParserResult:
        """Parse a file using the appropriate parser."""
        if isinstance(file_path, str):
            file_path = Path(file_path)

        parser = self.get_parser(file_path)
        if parser is None:
            return ParserResult.failure(f"No parser available for extension: {file_path.suffix}")

        if not file_path.exists():
            return ParserResult.failure(f"File not found: {file_path}")

        return parser.parse(file_path)

    @property
    def supported_extensions(self) -> list[str]:
        """Get list of all supported file extensions."""
        return list(self._extension_map.keys())


# Global registry instance with all parsers registered
_default_registry: ParserRegistry | None = None


def get_registry() -> ParserRegistry:
    """Get the default parser registry with all parsers registered."""
    global _default_registry
    if _default_registry is None:
        _default_registry = ParserRegistry()
        # Register all parsers
        _default_registry.register(DocxParser())
        _default_registry.register(EpubParser())
        _default_registry.register(PdfParser())
        _default_registry.register(PptxParser())
        _default_registry.register(TextParser())
        _default_registry.register(XlsxParser())
        _default_registry.register(OdpParser())
        _default_registry.register(OdtParser())
        _default_registry.register(OdsParser())
    return _default_registry


def get_parser(file_path: Path | str) -> BaseParser | None:
    """Get a parser for the given file."""
    return get_registry().get_parser(file_path)


def parse_file(file_path: Path | str) -> ParserResult:
    """Parse a file using the appropriate parser."""
    return get_registry().parse(file_path)


def can_parse(file_path: Path | str) -> bool:
    """Check if a file can be parsed."""
    return get_registry().can_parse(file_path)


def supported_extensions() -> list[str]:
    """Get list of supported file extensions."""
    return get_registry().supported_extensions
