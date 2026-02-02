"""Parser for OpenDocument Format files (.odp, .odt, .ods)."""

from pathlib import Path

from .base import BaseParser, ParserResult


class OdpParser(BaseParser):
    """Parser for OpenDocument Presentation (.odp) files using odfpy."""

    extensions = [".odp"]

    def parse(self, file_path: Path) -> ParserResult:
        """Parse an ODP presentation to Markdown."""
        try:
            from odf import text as odf_text
            from odf.opendocument import load
            from odf.draw import Frame, Page
            from odf.text import P
        except ImportError:
            return ParserResult.failure("odfpy not installed. Run: pip install odfpy")

        try:
            doc = load(file_path)
        except Exception as e:
            return ParserResult.failure(f"Failed to open ODP file: {e}")

        lines = []
        metadata = {}

        # Extract metadata
        try:
            meta = doc.meta
            if meta:
                title_elem = meta.getElementsByType(odf_text.Title)
                if title_elem:
                    metadata["title"] = self._get_text(title_elem[0])
        except Exception:
            pass

        # Add title
        if metadata.get("title"):
            lines.append(f"# {metadata['title']}")
        else:
            lines.append(f"# {file_path.stem}")
        lines.append("")

        # Get all pages (slides)
        pages = doc.getElementsByType(Page)

        for slide_num, page in enumerate(pages, 1):
            slide_title = page.getAttribute("name") or f"Slide {slide_num}"
            lines.append(f"## {slide_title}")
            lines.append("")

            # Get all text content from the page
            paragraphs = page.getElementsByType(P)
            has_content = False

            for para in paragraphs:
                text = self._get_text(para).strip()
                if text:
                    has_content = True
                    lines.append(f"- {text}")

            if not has_content:
                lines.append("*(No text content)*")

            lines.append("")
            lines.append("---")
            lines.append("")

        content = "\n".join(lines).strip()
        return ParserResult(content=content, metadata=metadata)

    def _get_text(self, element) -> str:
        """Recursively extract text from an ODF element."""
        text_parts = []
        if hasattr(element, "childNodes"):
            for child in element.childNodes:
                if child.nodeType == child.TEXT_NODE:
                    text_parts.append(child.data)
                else:
                    text_parts.append(self._get_text(child))
        return "".join(text_parts)


class OdtParser(BaseParser):
    """Parser for OpenDocument Text (.odt) files using odfpy."""

    extensions = [".odt"]

    def parse(self, file_path: Path) -> ParserResult:
        """Parse an ODT document to Markdown."""
        try:
            from odf import text as odf_text
            from odf.opendocument import load
            from odf.text import H, P, List, ListItem
            from odf.table import Table, TableRow, TableCell
        except ImportError:
            return ParserResult.failure("odfpy not installed. Run: pip install odfpy")

        try:
            doc = load(file_path)
        except Exception as e:
            return ParserResult.failure(f"Failed to open ODT file: {e}")

        lines = []
        metadata = {}

        # Extract metadata
        try:
            meta = doc.meta
            if meta:
                title_elem = meta.getElementsByType(odf_text.Title)
                if title_elem:
                    metadata["title"] = self._get_text(title_elem[0])
        except Exception:
            pass

        # Add title
        if metadata.get("title"):
            lines.append(f"# {metadata['title']}")
            lines.append("")

        # Process body
        body = doc.body
        if body:
            # Process headings
            for heading in body.getElementsByType(H):
                text = self._get_text(heading).strip()
                if text:
                    level = int(heading.getAttribute("outlinelevel") or 1)
                    lines.append(f"{'#' * level} {text}")
                    lines.append("")

            # Process paragraphs
            for para in body.getElementsByType(P):
                text = self._get_text(para).strip()
                if text:
                    lines.append(text)
                    lines.append("")

            # Process tables
            for table in body.getElementsByType(Table):
                table_md = self._table_to_markdown(table)
                if table_md:
                    lines.append(table_md)
                    lines.append("")

        content = "\n".join(lines).strip()
        return ParserResult(content=content, metadata=metadata)

    def _get_text(self, element) -> str:
        """Recursively extract text from an ODF element."""
        text_parts = []
        if hasattr(element, "childNodes"):
            for child in element.childNodes:
                if child.nodeType == child.TEXT_NODE:
                    text_parts.append(child.data)
                else:
                    text_parts.append(self._get_text(child))
        return "".join(text_parts)

    def _table_to_markdown(self, table) -> str:
        """Convert an ODF table to Markdown format."""
        from odf.table import TableRow, TableCell

        rows = []
        for row in table.getElementsByType(TableRow):
            cells = []
            for cell in row.getElementsByType(TableCell):
                cell_text = self._get_text(cell).strip().replace("\n", " ")
                cells.append(cell_text)
            if cells:
                rows.append(cells)

        if not rows:
            return ""

        # Normalize column count
        max_cols = max(len(row) for row in rows)
        for row in rows:
            while len(row) < max_cols:
                row.append("")

        # Build markdown table
        lines = []
        lines.append("| " + " | ".join(rows[0]) + " |")
        lines.append("| " + " | ".join(["---"] * max_cols) + " |")
        for row in rows[1:]:
            lines.append("| " + " | ".join(row) + " |")

        return "\n".join(lines)


class OdsParser(BaseParser):
    """Parser for OpenDocument Spreadsheet (.ods) files using odfpy."""

    extensions = [".ods"]

    def parse(self, file_path: Path) -> ParserResult:
        """Parse an ODS spreadsheet to Markdown (all sheets)."""
        try:
            from odf import text as odf_text
            from odf.opendocument import load
            from odf.table import Table, TableRow, TableCell
        except ImportError:
            return ParserResult.failure("odfpy not installed. Run: pip install odfpy")

        try:
            doc = load(file_path)
        except Exception as e:
            return ParserResult.failure(f"Failed to open ODS file: {e}")

        lines = []
        metadata = {}

        # Extract metadata
        try:
            meta = doc.meta
            if meta:
                title_elem = meta.getElementsByType(odf_text.Title)
                if title_elem:
                    metadata["title"] = self._get_text(title_elem[0])
        except Exception:
            pass

        # Add title
        if metadata.get("title"):
            lines.append(f"# {metadata['title']}")
        else:
            lines.append(f"# {file_path.stem}")
        lines.append("")

        # Process all tables (sheets)
        tables = doc.getElementsByType(Table)
        sheet_names = []

        for table in tables:
            sheet_name = table.getAttribute("name") or f"Sheet {len(sheet_names) + 1}"
            sheet_names.append(sheet_name)

            lines.append(f"## Sheet: {sheet_name}")
            lines.append("")

            rows_data = []
            max_col = 0

            for row in table.getElementsByType(TableRow):
                row_values = []
                has_content = False

                for cell in row.getElementsByType(TableCell):
                    # Handle repeated cells
                    repeat = int(cell.getAttribute("numbercolumnsrepeated") or 1)
                    cell_text = self._get_text(cell).strip().replace("\n", " ").replace("|", "\\|")

                    if cell_text:
                        has_content = True

                    for _ in range(min(repeat, 100)):  # Limit repeats
                        row_values.append(cell_text)

                if has_content:
                    rows_data.append(row_values)
                    max_col = max(max_col, len(row_values))

            if not rows_data:
                lines.append("*(Empty sheet)*")
                lines.append("")
                continue

            # Normalize rows
            for row in rows_data:
                while len(row) < max_col:
                    row.append("")
                # Trim trailing empty columns
                while row and not row[-1]:
                    row.pop()

            # Recalculate max_col after trimming
            max_col = max(len(row) for row in rows_data) if rows_data else 0

            if max_col == 0:
                lines.append("*(Empty sheet)*")
                lines.append("")
                continue

            # Normalize again after trimming
            for row in rows_data:
                while len(row) < max_col:
                    row.append("")

            # Build markdown table
            lines.append("| " + " | ".join(rows_data[0]) + " |")
            lines.append("| " + " | ".join(["---"] * max_col) + " |")
            for row in rows_data[1:]:
                lines.append("| " + " | ".join(row) + " |")

            lines.append("")

        metadata["sheets"] = sheet_names

        content = "\n".join(lines).strip()
        return ParserResult(content=content, metadata=metadata)

    def _get_text(self, element) -> str:
        """Recursively extract text from an ODF element."""
        text_parts = []
        if hasattr(element, "childNodes"):
            for child in element.childNodes:
                if child.nodeType == child.TEXT_NODE:
                    text_parts.append(child.data)
                else:
                    text_parts.append(self._get_text(child))
        return "".join(text_parts)
