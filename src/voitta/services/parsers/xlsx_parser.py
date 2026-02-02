"""Parser for Microsoft Excel documents (.xlsx)."""

from pathlib import Path

from .base import BaseParser, ParserResult


class XlsxParser(BaseParser):
    """Parser for .xlsx files using openpyxl."""

    extensions = [".xlsx"]

    def parse(self, file_path: Path) -> ParserResult:
        """Parse an Excel workbook to Markdown (all sheets)."""
        try:
            from openpyxl import load_workbook
            from openpyxl.utils.exceptions import InvalidFileException
        except ImportError:
            return ParserResult.failure("openpyxl not installed. Run: pip install openpyxl")

        try:
            wb = load_workbook(file_path, read_only=True, data_only=True)
        except InvalidFileException:
            return ParserResult.failure(f"Invalid or corrupted XLSX file: {file_path}")
        except Exception as e:
            return ParserResult.failure(f"Failed to open XLSX file: {e}")

        lines = []
        metadata = {}

        # Extract document properties
        try:
            props = wb.properties
            if props.title:
                metadata["title"] = props.title
            if props.creator:
                metadata["author"] = props.creator
        except Exception:
            pass

        # Add workbook title
        if metadata.get("title"):
            lines.append(f"# {metadata['title']}")
        else:
            lines.append(f"# {file_path.stem}")
        lines.append("")

        # Process all sheets
        sheet_names = wb.sheetnames
        metadata["sheets"] = sheet_names

        for sheet_name in sheet_names:
            sheet = wb[sheet_name]

            lines.append(f"## Sheet: {sheet_name}")
            lines.append("")

            # Get all rows with data
            rows_data = []
            max_col = 0

            for row in sheet.iter_rows():
                row_values = []
                has_content = False
                for cell in row:
                    value = cell.value
                    if value is not None:
                        has_content = True
                        # Convert to string and clean
                        str_value = str(value).strip().replace("\n", " ").replace("|", "\\|")
                        row_values.append(str_value)
                    else:
                        row_values.append("")

                if has_content:
                    rows_data.append(row_values)
                    max_col = max(max_col, len(row_values))

            if not rows_data:
                lines.append("*(Empty sheet)*")
                lines.append("")
                continue

            # Normalize all rows to same column count
            for row in rows_data:
                while len(row) < max_col:
                    row.append("")

            # Build markdown table
            # Header row (first row)
            header = rows_data[0] if rows_data else []
            lines.append("| " + " | ".join(header) + " |")

            # Separator
            lines.append("| " + " | ".join(["---"] * max_col) + " |")

            # Data rows
            for row in rows_data[1:]:
                lines.append("| " + " | ".join(row) + " |")

            lines.append("")

        wb.close()

        content = "\n".join(lines).strip()
        return ParserResult(content=content, metadata=metadata)
