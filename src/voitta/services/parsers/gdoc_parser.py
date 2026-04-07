"""Parser for Google Workspace stub files (.gdoc, .gsheet, .gslides).

These are JSON files created by the Google Drive Desktop app containing
a doc_id that maps to the original Google Docs/Sheets/Slides URL.
The parser extracts the source URL and returns the document title
(derived from filename) as searchable content.
"""

import json
from pathlib import Path

from .base import BaseParser, ParserResult

# Map file extension to Google Workspace URL template
_URL_TEMPLATES = {
    ".gdoc": "https://docs.google.com/document/d/{doc_id}/edit",
    ".gsheet": "https://docs.google.com/spreadsheets/d/{doc_id}/edit",
    ".gslides": "https://docs.google.com/presentation/d/{doc_id}/edit",
}


class GdocParser(BaseParser):
    """Parser for Google Drive Desktop stub files."""

    extensions = [".gdoc", ".gsheet", ".gslides"]

    def parse(self, file_path: Path) -> ParserResult:
        """Parse a Google Workspace stub file.

        Reads the JSON to extract doc_id, constructs the source URL,
        and returns the document title as content for indexing.
        """
        try:
            raw = file_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            return ParserResult.failure(f"Failed to read Google stub file: {e}")

        doc_id = data.get("doc_id")
        if not doc_id:
            return ParserResult.failure("No doc_id found in Google stub file")

        ext = file_path.suffix.lower()
        url_template = _URL_TEMPLATES.get(ext)
        source_url = url_template.format(doc_id=doc_id) if url_template else None

        # Use the filename (without extension) as searchable content
        title = file_path.stem

        metadata = {}
        if source_url:
            metadata["source_url"] = source_url
        if doc_id:
            metadata["google_doc_id"] = doc_id

        return ParserResult(
            content=title,
            metadata=metadata,
        )
