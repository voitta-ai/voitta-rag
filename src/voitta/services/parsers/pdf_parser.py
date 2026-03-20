"""PDF parser using MinerU for high-quality markdown extraction.

Supports bucketed processing for large PDFs - splits into smaller parts (buckets)
and processes each separately for better reliability and progress feedback.
"""

import logging
import tempfile
import time
from pathlib import Path
from typing import Generator

import fitz  # PyMuPDF

from .base import BaseParser, ParserResult
from ...config import get_settings

logger = logging.getLogger(__name__)


def get_bucket_settings() -> tuple[int, int]:
    """Get PDF bucketing settings from config.

    Returns (pages_per_bucket, min_pages_for_bucketing).
    min_pages_for_bucketing is set to 20% more than pages_per_bucket
    to avoid bucketing PDFs that would only produce 1 bucket + a few pages.
    """
    settings = get_settings()
    pages_per_bucket = settings.pdf_pages_per_bucket
    min_pages_for_bucketing = int(pages_per_bucket * 1.2)
    return pages_per_bucket, min_pages_for_bucketing


def get_pdf_page_count(file_path: Path) -> int:
    """Get the number of pages in a PDF."""
    try:
        doc = fitz.open(file_path)
        count = len(doc)
        doc.close()
        return count
    except Exception as e:
        logger.warning(f"Could not get page count for {file_path}: {e}")
        return -1


def split_pdf(
    input_path: Path, output_dir: Path, pages_per_bucket: int
) -> list[tuple[Path, int, int]]:
    """Split a PDF into smaller buckets for processing.

    Returns list of (bucket_path, start_page, end_page) tuples.
    Pages are 1-indexed for display.
    """
    doc = fitz.open(input_path)
    total_pages = len(doc)
    buckets = []

    output_dir.mkdir(parents=True, exist_ok=True)

    for start_page in range(0, total_pages, pages_per_bucket):
        end_page = min(start_page + pages_per_bucket, total_pages)
        bucket_num = start_page // pages_per_bucket + 1

        bucket_path = output_dir / f"bucket_{bucket_num:03d}.pdf"

        bucket_doc = fitz.open()
        bucket_doc.insert_pdf(doc, from_page=start_page, to_page=end_page - 1)
        bucket_doc.save(str(bucket_path))
        bucket_doc.close()

        buckets.append((bucket_path, start_page + 1, end_page))
        logger.debug(f"Created bucket {bucket_num}: pages {start_page + 1}-{end_page}")

    doc.close()
    return buckets


def parse_single_pdf(file_path: Path, method: str, lang: str) -> ParserResult:
    """Parse a single PDF file (or bucket) using MinerU directly."""
    try:
        from mineru.cli.common import do_parse, read_fn
    except ImportError as e:
        return ParserResult.failure(f"MinerU not installed: {e}")

    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir) / "output"
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            pdf_bytes = read_fn(file_path)
            pdf_name = file_path.stem

            do_parse(
                output_dir=str(output_dir),
                pdf_file_names=[pdf_name],
                pdf_bytes_list=[pdf_bytes],
                p_lang_list=[lang],
                backend="pipeline",
                parse_method=method,
                formula_enable=True,
                table_enable=True,
                f_draw_layout_bbox=False,
                f_draw_span_bbox=False,
                f_dump_md=True,
                f_dump_middle_json=False,
                f_dump_model_output=False,
                f_dump_orig_pdf=False,
                f_dump_content_list=False,
            )
        except Exception as e:
            return ParserResult.failure(f"MinerU parsing failed: {e}")

        # Find the output markdown
        md_path = output_dir / pdf_name / method / f"{pdf_name}.md"
        if not md_path.exists():
            all_md = list(output_dir.rglob("*.md"))
            if all_md:
                md_path = all_md[0]
            else:
                return ParserResult.failure("No markdown output from MinerU")

        try:
            content = md_path.read_text(encoding="utf-8")
        except Exception as e:
            return ParserResult.failure(f"Failed to read output markdown: {e}")

        return ParserResult(content=content, metadata={})


class PdfParser(BaseParser):
    """Parser for PDF files using MinerU.

    Supports bucketed processing for large PDFs (splitting into smaller parts).
    """

    extensions = [".pdf"]

    def __init__(self, method: str = "auto", lang: str = "en"):
        self.method = method
        self.lang = lang

    def parse(self, file_path: Path) -> ParserResult:
        """Parse a PDF file. For large PDFs, use parse_in_buckets() instead."""
        results = list(self.parse_in_buckets(file_path))
        if not results:
            return ParserResult.failure("No results from parsing")

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
                "buckets_processed": len(results),
            },
        )

    def parse_in_buckets(self, file_path: Path) -> Generator[ParserResult, None, None]:
        """Parse a PDF file in buckets, yielding results as they complete."""
        if not file_path.exists():
            yield ParserResult.failure(f"File not found: {file_path}")
            return

        page_count = get_pdf_page_count(file_path)
        pages_per_bucket, min_pages_for_bucketing = get_bucket_settings()
        use_bucketing = page_count > min_pages_for_bucketing and page_count > 0

        logger.info(
            f"PDF parse: {file_path.name} ({page_count} pages, "
            f"{'bucketed' if use_bucketing else 'single'})"
        )

        if use_bucketing:
            with tempfile.TemporaryDirectory() as tmpdir:
                buckets_dir = Path(tmpdir) / "buckets"
                buckets = split_pdf(file_path, buckets_dir, pages_per_bucket)

                if not buckets:
                    yield ParserResult.failure("Failed to split PDF into buckets")
                    return

                total_buckets = len(buckets)
                logger.info(f"Split into {total_buckets} buckets")

                for i, (bucket_path, start_page, end_page) in enumerate(buckets):
                    start_time = time.time()
                    result = parse_single_pdf(bucket_path, self.method, self.lang)
                    elapsed = time.time() - start_time

                    if result.success:
                        logger.info(
                            f"Bucket {i + 1}/{total_buckets} done: "
                            f"{len(result.content)} chars in {elapsed:.1f}s"
                        )
                    else:
                        logger.error(f"Bucket {i + 1}/{total_buckets} failed: {result.error}")

                    result.metadata = {
                        "source_format": "pdf",
                        "parser": "mineru",
                        "filename": file_path.name,
                        "bucket_index": i,
                        "total_buckets": total_buckets,
                        "start_page": start_page,
                        "end_page": end_page,
                        "source_page_count": page_count,
                        "parse_time_seconds": elapsed,
                    }
                    yield result
        else:
            start_time = time.time()
            result = parse_single_pdf(file_path, self.method, self.lang)
            elapsed = time.time() - start_time

            if result.success:
                logger.info(f"PDF parsed: {len(result.content)} chars in {elapsed:.1f}s")
            else:
                logger.error(f"PDF parse failed: {result.error}")

            result.metadata = {
                "source_format": "pdf",
                "parser": "mineru",
                "filename": file_path.name,
                "bucket_index": 0,
                "total_buckets": 1,
                "start_page": 1,
                "end_page": page_count if page_count > 0 else None,
                "source_page_count": page_count if page_count > 0 else None,
                "parse_time_seconds": elapsed,
            }
            yield result
