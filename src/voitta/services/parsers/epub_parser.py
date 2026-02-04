"""EPUB parser using pandoc for markdown extraction."""

import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import BaseParser, ParserResult


class EpubParser(BaseParser):
    """Parser for EPUB files using pandoc."""

    extensions = [".epub"]

    def parse(self, file_path: Path) -> ParserResult:
        """Parse an EPUB file using pandoc.

        Args:
            file_path: Path to the EPUB file.

        Returns:
            ParserResult with Markdown content or error.
        """
        if not file_path.exists():
            return ParserResult.failure(f"File not found: {file_path}")

        if not shutil.which("pandoc"):
            return ParserResult.failure(
                "pandoc not found. Please install it: sudo apt install pandoc"
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = Path(tmpdir) / "output.md"

            cmd = [
                "pandoc",
                str(file_path),
                "-o", str(output_file),
                "--wrap=none",
            ]

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=300,  # 5 minute timeout
                )
            except subprocess.TimeoutExpired:
                return ParserResult.failure("pandoc timed out after 5 minutes")
            except Exception as e:
                return ParserResult.failure(f"Failed to run pandoc: {e}")

            if result.returncode != 0:
                return ParserResult.failure(
                    f"pandoc failed with exit code {result.returncode}: {result.stderr}"
                )

            if not output_file.exists():
                return ParserResult.failure("pandoc did not produce output file")

            try:
                content = output_file.read_text(encoding="utf-8")
            except Exception as e:
                return ParserResult.failure(f"Failed to read output markdown: {e}")

            return ParserResult(
                content=content,
                metadata={
                    "source_format": "epub",
                    "parser": "pandoc",
                    "filename": file_path.name,
                },
            )
