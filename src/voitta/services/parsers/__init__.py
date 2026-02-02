"""Document parsers for extracting text content as Markdown."""

from .base import BaseParser, ParserResult
from .registry import (
    ParserRegistry,
    can_parse,
    get_parser,
    get_registry,
    parse_file,
    supported_extensions,
)

__all__ = [
    "BaseParser",
    "ParserResult",
    "ParserRegistry",
    "can_parse",
    "get_parser",
    "get_registry",
    "parse_file",
    "supported_extensions",
]
