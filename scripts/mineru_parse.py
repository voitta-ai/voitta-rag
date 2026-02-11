#!/usr/bin/env python
"""MinerU PDF parsing wrapper script.

This script is meant to be run with the .mineru-venv virtualenv.
It uses the MinerU Python API to parse PDFs and output markdown.

Usage:
    .mineru-venv/bin/python scripts/mineru_parse.py <input_pdf> <output_dir> [options]

Logs are written to: logs/mineru_worker.log
"""

import argparse
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# Auto-detect GPU: use CUDA if available, fall back to CPU
try:
    import torch
    if torch.cuda.is_available():
        os.environ.setdefault("MINERU_DEVICE_MODE", "cuda")
    else:
        os.environ.setdefault("MINERU_DEVICE_MODE", "cpu")
except ImportError:
    os.environ.setdefault("MINERU_DEVICE_MODE", "cpu")

# Set up file logging
SCRIPT_DIR = Path(__file__).parent.parent
LOG_FILE = SCRIPT_DIR / "logs" / "mineru_worker.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# Create logger
logger = logging.getLogger("mineru_worker")
logger.setLevel(logging.DEBUG)

# Remove existing handlers
for handler in logger.handlers[:]:
    logger.removeHandler(handler)

# File handler
file_handler = logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8')
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s.%(msecs)03d | %(levelname)-7s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))
logger.addHandler(file_handler)


def get_memory_mb() -> float:
    """Get current process memory usage in MB."""
    try:
        import psutil
        process = psutil.Process(os.getpid())
        return process.memory_info().rss / 1024 / 1024
    except ImportError:
        try:
            with open('/proc/self/status') as f:
                for line in f:
                    if line.startswith('VmRSS:'):
                        return int(line.split()[1]) / 1024
        except:
            pass
    return 0


def get_pdf_page_count(pdf_path: Path) -> int:
    """Get the number of pages in a PDF using PyMuPDF."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(pdf_path)
        count = len(doc)
        doc.close()
        return count
    except ImportError:
        logger.warning("PyMuPDF not available, cannot get page count")
        return -1
    except Exception as e:
        logger.warning(f"Could not get page count: {e}")
        return -1


def parse_pdf(
    input_path: Path,
    output_dir: Path,
    method: str = "auto",
    lang: str = "en",
    backend: str = "pipeline",
) -> dict:
    """Parse a PDF file using MinerU API."""
    total_start = time.time()

    logger.info("=" * 70)
    logger.info("MINERU WORKER - PDF PARSING STARTED")
    logger.info("=" * 70)
    logger.info(f"PID: {os.getpid()}")
    logger.info(f"Input: {input_path}")
    logger.info(f"Output dir: {output_dir}")
    logger.info(f"Method: {method}")
    logger.info(f"Language: {lang}")
    logger.info(f"Backend: {backend}")
    logger.info(f"Device mode: {os.environ.get('MINERU_DEVICE_MODE', 'not set')}")
    logger.info(f"Initial memory: {get_memory_mb():.1f} MB")

    # Get PDF info
    file_size_mb = input_path.stat().st_size / 1024 / 1024
    page_count = get_pdf_page_count(input_path)
    logger.info(f"PDF size: {file_size_mb:.2f} MB")
    logger.info(f"PDF pages: {page_count}")

    logger.info("-" * 70)
    logger.info("PHASE 1: Importing MinerU modules...")
    import_start = time.time()

    try:
        from mineru.cli.common import do_parse, read_fn
        logger.info(f"MinerU import completed in {time.time() - import_start:.1f}s")
        logger.info(f"Memory after import: {get_memory_mb():.1f} MB")
    except ImportError as e:
        logger.error(f"FATAL: Failed to import MinerU: {e}")
        logger.error(traceback.format_exc())
        return {"success": False, "error": f"Failed to import MinerU: {e}"}

    try:
        logger.info("-" * 70)
        logger.info("PHASE 2: Reading PDF file...")
        read_start = time.time()
        pdf_bytes = read_fn(input_path)
        pdf_name = input_path.stem
        logger.info(f"PDF read completed in {time.time() - read_start:.1f}s")
        logger.info(f"PDF bytes: {len(pdf_bytes)} ({len(pdf_bytes)/1024/1024:.2f} MB)")
        logger.info(f"Memory after read: {get_memory_mb():.1f} MB")

        logger.info("-" * 70)
        logger.info("PHASE 3: Creating output directory...")
        os.makedirs(output_dir, exist_ok=True)
        logger.info(f"Output directory created: {output_dir}")

        logger.info("-" * 70)
        logger.info("PHASE 4: Starting MinerU do_parse()...")
        logger.info(f"  formula_enable: True")
        logger.info(f"  table_enable: True")
        logger.info(f"  parse_method: {method}")
        logger.info(f"  backend: {backend}")
        parse_start = time.time()

        logger.info(f"Parsing started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        do_parse(
            output_dir=str(output_dir),
            pdf_file_names=[pdf_name],
            pdf_bytes_list=[pdf_bytes],
            p_lang_list=[lang],
            backend=backend,
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

        parse_elapsed = time.time() - parse_start
        logger.info(f"MinerU do_parse() completed!")
        logger.info(f"Parse time: {parse_elapsed:.1f}s ({parse_elapsed/60:.1f} minutes)")
        if page_count > 0:
            logger.info(f"Speed: {page_count / parse_elapsed:.2f} pages/second")
        logger.info(f"Memory after parse: {get_memory_mb():.1f} MB")

        logger.info("-" * 70)
        logger.info("PHASE 5: Locating output markdown file...")

        md_path = output_dir / pdf_name / method / f"{pdf_name}.md"
        logger.info(f"Expected path: {md_path}")
        logger.info(f"Expected path exists: {md_path.exists()}")

        if not md_path.exists():
            logger.warning("Expected path not found, searching for alternatives...")
            all_md_files = list(output_dir.rglob("*.md"))
            logger.info(f"Found {len(all_md_files)} .md files in output dir:")
            for f in all_md_files:
                logger.info(f"  - {f} ({f.stat().st_size} bytes)")

            if all_md_files:
                md_path = all_md_files[0]
                logger.info(f"Using alternative: {md_path}")

        if md_path.exists():
            md_size = md_path.stat().st_size
            md_content = md_path.read_text(encoding='utf-8')
            md_lines = md_content.count('\n')

            total_elapsed = time.time() - total_start

            logger.info("-" * 70)
            logger.info("SUCCESS! PARSING COMPLETE")
            logger.info("-" * 70)
            logger.info(f"Output file: {md_path}")
            logger.info(f"Output size: {md_size} bytes ({md_size/1024:.1f} KB)")
            logger.info(f"Output lines: {md_lines}")
            logger.info(f"Parse time: {parse_elapsed:.1f}s ({parse_elapsed/60:.1f} min)")
            logger.info(f"Total time: {total_elapsed:.1f}s ({total_elapsed/60:.1f} min)")
            logger.info(f"Final memory: {get_memory_mb():.1f} MB")
            logger.info("=" * 70)
            logger.info("")

            return {
                "success": True,
                "markdown_path": str(md_path),
                "stats": {
                    "parse_time_seconds": parse_elapsed,
                    "total_time_seconds": total_elapsed,
                    "page_count": page_count,
                    "output_size_bytes": md_size,
                    "output_lines": md_lines,
                    "memory_mb": get_memory_mb(),
                }
            }
        else:
            logger.error("FAILED: No markdown file found after parsing!")
            logger.error("Contents of output directory:")
            try:
                for f in output_dir.rglob("*"):
                    logger.error(f"  {f}")
            except Exception as e:
                logger.error(f"Could not list output dir: {e}")

            return {
                "success": False,
                "error": "Markdown file not found after parsing",
            }

    except Exception as e:
        elapsed = time.time() - total_start
        logger.error(f"FATAL ERROR after {elapsed:.1f}s!")
        logger.error(f"Exception: {e}")
        logger.error(f"Type: {type(e).__name__}")
        logger.error(f"Memory at error: {get_memory_mb():.1f} MB")
        logger.error(f"Traceback:\n{traceback.format_exc()}")
        return {
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc(),
        }


def main():
    parser = argparse.ArgumentParser(description="Parse PDF to Markdown using MinerU")
    parser.add_argument("input_path", type=Path, help="Path to PDF file")
    parser.add_argument("output_dir", type=Path, help="Output directory")
    parser.add_argument("--method", default="auto", choices=["auto", "txt", "ocr"])
    parser.add_argument("--lang", default="en")
    parser.add_argument("--backend", default="pipeline")

    args = parser.parse_args()

    logger.info(f"Script invoked with: {sys.argv}")
    logger.info(f"Working directory: {os.getcwd()}")

    result = parse_pdf(
        input_path=args.input_path,
        output_dir=args.output_dir,
        method=args.method,
        lang=args.lang,
        backend=args.backend,
    )

    # Output JSON result to stdout (parent process reads this)
    print(json.dumps(result))

    if result["success"]:
        logger.info("Exiting with success (code 0)")
    else:
        logger.error(f"Exiting with failure (code 1): {result.get('error', 'unknown')}")

    sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    main()
