"""AZW3/MOBI parser using mobi (KindleUnpack) for content extraction."""

import logging
import shutil
import tempfile
from pathlib import Path

from .base import BaseParser, ParserResult

logger = logging.getLogger(__name__)


class Azw3Parser(BaseParser):
    """Parser for AZW3, MOBI, and AZW files using the mobi library."""

    extensions = [".azw3", ".azw", ".mobi"]

    def parse(self, file_path: Path) -> ParserResult:
        """Parse an AZW3/MOBI file by extracting HTML and converting to Markdown.

        Args:
            file_path: Path to the ebook file.

        Returns:
            ParserResult with Markdown content or error.
        """
        if not file_path.exists():
            return ParserResult.failure(f"File not found: {file_path}")

        try:
            import mobi
        except ImportError:
            return ParserResult.failure("mobi not installed. Run: pip install mobi")

        try:
            import html2text
        except ImportError:
            return ParserResult.failure("html2text not installed. Run: pip install html2text")

        tempdir = None
        try:
            tempdir, extracted_path = mobi.extract(str(file_path))
            extracted = Path(extracted_path)

            if not extracted.exists():
                return ParserResult.failure(f"Extraction produced no output for: {file_path.name}")

            html_content = self._read_html(extracted)
            if not html_content:
                return ParserResult.failure(
                    f"No readable content found in extracted files: {file_path.name}"
                )

            converter = html2text.HTML2Text()
            converter.body_width = 0  # No wrapping
            converter.ignore_links = False
            converter.ignore_images = True
            converter.ignore_emphasis = False
            converter.unicode_snob = True

            markdown = converter.handle(html_content)

            return ParserResult(
                content=markdown.strip(),
                metadata={
                    "source_format": file_path.suffix.lstrip("."),
                    "parser": "mobi",
                    "filename": file_path.name,
                },
            )

        except Exception as e:
            logger.exception(f"Failed to parse {file_path.name}")
            return ParserResult.failure(f"Failed to parse {file_path.name}: {e}")
        finally:
            if tempdir:
                shutil.rmtree(tempdir, ignore_errors=True)

    def _read_html(self, extracted: Path) -> str | None:
        """Read HTML content from the extracted path.

        mobi.extract() returns a path that may be a single HTML file
        or a directory containing multiple HTML files.
        """
        if extracted.is_file():
            return self._read_file(extracted)

        # Directory — collect all HTML files in reading order
        html_files = sorted(extracted.rglob("*.html")) + sorted(extracted.rglob("*.xhtml"))
        if not html_files:
            # Fallback: try any file that looks like markup
            html_files = sorted(extracted.rglob("*.htm"))

        if not html_files:
            return None

        parts = []
        for hf in html_files:
            content = self._read_file(hf)
            if content:
                parts.append(content)

        return "\n".join(parts) if parts else None

    @staticmethod
    def _read_file(path: Path) -> str | None:
        """Read a file with encoding fallback."""
        for encoding in ("utf-8", "latin-1"):
            try:
                return path.read_text(encoding=encoding)
            except (UnicodeDecodeError, ValueError):
                continue
        return None
