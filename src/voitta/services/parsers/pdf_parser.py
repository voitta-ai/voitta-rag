"""PDF parser using MinerU for high-quality markdown extraction.

Supports chunked processing for large PDFs - splits into smaller parts
and processes each separately for better reliability and progress feedback.
"""

import json
import logging
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Generator

from .base import BaseParser, ParserResult

# Set up file logging for PDF parsing
LOG_FILE = Path(__file__).parent.parent.parent.parent.parent / "logs" / "pdf_parsing.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

pdf_logger = logging.getLogger("voitta.pdf_parser")
pdf_logger.setLevel(logging.DEBUG)

for handler in pdf_logger.handlers[:]:
    pdf_logger.removeHandler(handler)

file_handler = logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8')
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s.%(msecs)03d | %(levelname)-7s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))
pdf_logger.addHandler(file_handler)

logger = logging.getLogger(__name__)

# Path to MinerU venv and wrapper script relative to project root
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent.parent
MINERU_PYTHON = PROJECT_ROOT / ".mineru-venv" / "bin" / "python"
MINERU_SCRIPT = PROJECT_ROOT / "scripts" / "mineru_parse.py"

# Chunking settings
PAGES_PER_CHUNK = 50  # Process this many pages at a time for large PDFs
MIN_PAGES_FOR_CHUNKING = 60  # Only chunk if more than this many pages


def get_pdf_page_count(file_path: Path) -> int:
    """Get the number of pages in a PDF."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(file_path)
        count = len(doc)
        doc.close()
        return count
    except ImportError:
        pdf_logger.warning("PyMuPDF not installed in main env, cannot get page count")
        return -1
    except Exception as e:
        pdf_logger.warning(f"Could not get page count: {e}")
        return -1


def split_pdf(input_path: Path, output_dir: Path, pages_per_chunk: int) -> list[tuple[Path, int, int]]:
    """Split a PDF into smaller chunks.

    Returns list of (chunk_path, start_page, end_page) tuples.
    Pages are 1-indexed for display.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        pdf_logger.error("PyMuPDF not installed - cannot split PDF")
        return []

    doc = fitz.open(input_path)
    total_pages = len(doc)
    chunks = []

    output_dir.mkdir(parents=True, exist_ok=True)

    for start_page in range(0, total_pages, pages_per_chunk):
        end_page = min(start_page + pages_per_chunk, total_pages)
        chunk_num = start_page // pages_per_chunk + 1

        chunk_path = output_dir / f"chunk_{chunk_num:03d}.pdf"

        # Create a new PDF with just these pages
        chunk_doc = fitz.open()
        chunk_doc.insert_pdf(doc, from_page=start_page, to_page=end_page - 1)
        chunk_doc.save(str(chunk_path))
        chunk_doc.close()

        # Use 1-indexed pages for display
        chunks.append((chunk_path, start_page + 1, end_page))
        pdf_logger.info(f"Created chunk {chunk_num}: pages {start_page + 1}-{end_page}")

    doc.close()
    return chunks


def parse_single_pdf(file_path: Path, method: str, lang: str) -> ParserResult:
    """Parse a single PDF file (or chunk) using MinerU subprocess."""
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir) / "output"

        cmd = [
            str(MINERU_PYTHON),
            str(MINERU_SCRIPT),
            str(file_path),
            str(output_dir),
            "--method", method,
            "--lang", lang,
            "--backend", "pipeline",
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600,  # 1 hour per chunk
            )
        except subprocess.TimeoutExpired:
            return ParserResult.failure("MinerU timed out after 1 hour")
        except Exception as e:
            return ParserResult.failure(f"Failed to run MinerU: {e}")

        # Parse JSON result
        try:
            parse_result = json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            if result.returncode != 0:
                return ParserResult.failure(
                    f"MinerU failed with exit code {result.returncode}: {result.stderr[:500]}"
                )
            return ParserResult.failure(f"Failed to parse MinerU output: {result.stdout[:500]}")

        if not parse_result.get("success"):
            return ParserResult.failure(
                parse_result.get("error", "Unknown error from MinerU")
            )

        md_path = Path(parse_result["markdown_path"])
        if not md_path.exists():
            return ParserResult.failure(f"Markdown file not found: {md_path}")

        try:
            content = md_path.read_text(encoding="utf-8")
        except Exception as e:
            return ParserResult.failure(f"Failed to read output markdown: {e}")

        return ParserResult(
            content=content,
            metadata=parse_result.get("stats", {}),
        )


class PdfParser(BaseParser):
    """Parser for PDF files using MinerU.

    Supports chunked processing for large PDFs.
    """

    extensions = [".pdf"]

    def __init__(self, method: str = "auto", lang: str = "en"):
        self.method = method
        self.lang = lang

    def parse(self, file_path: Path) -> ParserResult:
        """Parse a PDF file. For large PDFs, use parse_chunked() instead."""
        # For backward compatibility, parse the whole file
        results = list(self.parse_chunked(file_path))
        if not results:
            return ParserResult.failure("No results from parsing")

        # Combine all chunk results
        all_content = []
        all_errors = []

        for result in results:
            if result.success:
                all_content.append(result.content)
            else:
                all_errors.append(result.error)

        if not all_content:
            return ParserResult.failure("; ".join(all_errors))

        return ParserResult(
            content="\n\n".join(all_content),
            metadata={
                "source_format": "pdf",
                "parser": "mineru",
                "filename": file_path.name,
                "chunks_processed": len(results),
            },
        )

    def parse_chunked(self, file_path: Path) -> Generator[ParserResult, None, None]:
        """Parse a PDF file in chunks, yielding results as they complete.

        For small PDFs, yields a single result.
        For large PDFs, yields one result per chunk.

        Each result has metadata with 'chunk_index' and 'total_chunks'.
        """
        file_size_mb = file_path.stat().st_size / 1024 / 1024 if file_path.exists() else 0

        pdf_logger.info("=" * 70)
        pdf_logger.info("STARTING PDF PARSE")
        pdf_logger.info("=" * 70)
        pdf_logger.info(f"File: {file_path.name}")
        pdf_logger.info(f"Size: {file_size_mb:.2f} MB")
        pdf_logger.info(f"Method: {self.method}, Lang: {self.lang}")

        if not file_path.exists():
            pdf_logger.error(f"File not found: {file_path}")
            yield ParserResult.failure(f"File not found: {file_path}")
            return

        if not MINERU_PYTHON.exists():
            pdf_logger.error(f"MinerU venv not found at {MINERU_PYTHON}")
            yield ParserResult.failure(f"MinerU venv not found at {MINERU_PYTHON}")
            return

        if not MINERU_SCRIPT.exists():
            pdf_logger.error(f"MinerU script not found at {MINERU_SCRIPT}")
            yield ParserResult.failure(f"MinerU script not found at {MINERU_SCRIPT}")
            return

        # Get page count to decide on chunking
        page_count = get_pdf_page_count(file_path)
        pdf_logger.info(f"Page count: {page_count}")

        use_chunking = page_count > MIN_PAGES_FOR_CHUNKING and page_count > 0

        if use_chunking:
            pdf_logger.info(f"Using CHUNKED processing ({page_count} pages, {PAGES_PER_CHUNK} per chunk)")

            with tempfile.TemporaryDirectory() as tmpdir:
                chunks_dir = Path(tmpdir) / "chunks"
                chunks = split_pdf(file_path, chunks_dir, PAGES_PER_CHUNK)

                if not chunks:
                    pdf_logger.error("Failed to split PDF")
                    yield ParserResult.failure("Failed to split PDF into chunks")
                    return

                total_chunks = len(chunks)
                pdf_logger.info(f"Split into {total_chunks} chunks")

                for i, (chunk_path, start_page, end_page) in enumerate(chunks):
                    chunk_start = time.time()
                    chunk_num = i + 1

                    pdf_logger.info(f"Processing chunk {chunk_num}/{total_chunks} (pages {start_page}-{end_page})")

                    result = parse_single_pdf(chunk_path, self.method, self.lang)
                    elapsed = time.time() - chunk_start

                    if result.success:
                        pdf_logger.info(f"Chunk {chunk_num} SUCCESS: {len(result.content)} chars in {elapsed:.1f}s")
                        # Add chunk metadata
                        result.metadata = {
                            "source_format": "pdf",
                            "parser": "mineru",
                            "filename": file_path.name,
                            "chunk_index": i,
                            "total_chunks": total_chunks,
                            "start_page": start_page,
                            "end_page": end_page,
                            "source_page_count": page_count,
                            "parse_time_seconds": elapsed,
                        }
                    else:
                        pdf_logger.error(f"Chunk {chunk_num} FAILED: {result.error}")
                        result.metadata = {
                            "chunk_index": i,
                            "total_chunks": total_chunks,
                        }

                    yield result

        else:
            # Small PDF - process as single file
            pdf_logger.info("Using STANDARD processing (small PDF)")

            start_time = time.time()
            result = parse_single_pdf(file_path, self.method, self.lang)
            elapsed = time.time() - start_time

            if result.success:
                pdf_logger.info(f"SUCCESS: {len(result.content)} chars in {elapsed:.1f}s")
                result.metadata = {
                    "source_format": "pdf",
                    "parser": "mineru",
                    "filename": file_path.name,
                    "chunk_index": 0,
                    "total_chunks": 1,
                    "start_page": 1,
                    "end_page": page_count if page_count > 0 else None,
                    "source_page_count": page_count if page_count > 0 else None,
                    "parse_time_seconds": elapsed,
                }
            else:
                pdf_logger.error(f"FAILED: {result.error}")
                result.metadata = {
                    "chunk_index": 0,
                    "total_chunks": 1,
                }

            yield result

        pdf_logger.info("=" * 70)
        pdf_logger.info("PDF PARSE COMPLETE")
        pdf_logger.info("=" * 70)
