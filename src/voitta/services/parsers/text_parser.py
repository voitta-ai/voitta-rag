"""Parser for plain text files (.txt, .md)."""

from pathlib import Path

from .base import BaseParser, ParserResult


class TextParser(BaseParser):
    """Parser for plain text files - returns content as-is."""

    extensions = [".txt", ".md"]

    def parse(self, file_path: Path) -> ParserResult:
        """Read a text file and return content unchanged."""
        try:
            content = file_path.read_text(encoding="utf-8")
            return ParserResult(content=content)
        except UnicodeDecodeError:
            try:
                content = file_path.read_text(encoding="latin-1")
                return ParserResult(content=content)
            except Exception as e:
                return ParserResult.failure(f"Failed to read text file: {e}")
        except Exception as e:
            return ParserResult.failure(f"Failed to read text file: {e}")
