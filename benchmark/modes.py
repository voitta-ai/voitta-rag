"""Context builders, one per benchmark mode.

Each builder takes (config, repo_root, question) and returns the context string
that gets injected into the answering prompt. Adding a mode means adding one
function here and one entry in BUILDERS -- nothing else in the harness changes.
"""

import asyncio
import fnmatch
import json
import os
import subprocess


def _iter_repo_files(repo_root, include_globs, exclude_globs):
    """Yield repo-relative paths matching include_globs and not exclude_globs."""
    matched = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for filename in filenames:
            absolute = os.path.join(dirpath, filename)
            relative = os.path.relpath(absolute, repo_root)
            if not any(fnmatch.fnmatch(relative, g) for g in include_globs):
                continue
            if any(fnmatch.fnmatch(relative, g) for g in exclude_globs):
                continue
            matched.append(relative)
    matched.sort()
    return matched


def build_baseline(config, repo_root, question):
    """Raw source dump, greedily packed up to a character budget.

    The budget is a packing heuristic only. Real token counts come from the
    API usage field on the answering call, never from an offline estimate.
    """
    repo = config["repo"]
    budget = config["baseline_char_budget"]
    paths = _iter_repo_files(
        repo_root, repo["include_globs"], repo.get("exclude_globs", [])
    )

    chunks = []
    used = 0
    included = 0
    for relative in paths:
        with open(os.path.join(repo_root, relative), "r", errors="replace") as handle:
            body = handle.read()
        block = "===== FILE: {0} =====\n{1}\n".format(relative, body)
        if used + len(block) > budget:
            continue
        chunks.append(block)
        used += len(block)
        included += 1

    header = (
        "Repository source dump. {0} of {1} matching files included "
        "(character budget {2}).\n\n".format(included, len(paths), budget)
    )
    retval = header + "".join(chunks)
    return retval


def build_llm_tldr(config, repo_root, question):
    """Structural context from the llm-tldr CLI.

    `tldr warm` and `tldr semantic index` are one-time per repo; run them via
    prepare_llm_tldr() before the benchmark rather than once per question, so
    index-build time is not charged to the first question's wall clock.
    """
    tldr_bin = config["tldr_bin"]

    search = subprocess.run(
        [
            tldr_bin,
            "semantic",
            "search",
            question["text"],
            "--path",
            repo_root,
            "--k",
            str(config["tldr_search_k"]),
            "--expand",
        ],
        capture_output=True,
        text=True,
    )
    if search.returncode != 0:
        raise RuntimeError(
            "tldr semantic search failed ({0}): {1}".format(
                search.returncode, search.stderr.strip()
            )
        )

    retval = "llm-tldr structural context:\n\n" + search.stdout
    return retval


def prepare_llm_tldr(config, repo_root):
    """One-time index build for the llm-tldr mode.

    `--lang` is mandatory in practice: without it `tldr semantic index` reports
    "Indexed 0 code units" and exits 0, so every later search returns nothing and
    the mode scores as a total retrieval failure rather than a setup error.
    """
    tldr_bin = config["tldr_bin"]
    lang = config["tldr_lang"]

    for command in (
        [tldr_bin, "warm", repo_root],
        [tldr_bin, "semantic", "index", repo_root, "--lang", lang],
    ):
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                "{0} failed ({1}): {2}".format(
                    " ".join(command[1:3]), result.returncode, result.stderr.strip()
                )
            )
        if "Indexed 0 code units" in result.stdout:
            raise RuntimeError(
                "tldr semantic index produced an empty index for lang={0}; "
                "check the language matches the repo".format(lang)
            )


async def _voitta_rag_search(url, query, limit, include_folders):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    arguments = {"query": query, "limit": limit}
    if include_folders:
        arguments["include_folders"] = include_folders

    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("search", arguments)

    parts = []
    for block in result.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    retval = "\n".join(parts)
    return retval


def build_voitta_rag(config, repo_root, question):
    """Retrieved chunks from a running voitta-rag instance over MCP."""
    raw = asyncio.run(
        _voitta_rag_search(
            config["voitta_rag_mcp_url"],
            question["text"],
            config["voitta_rag_search_limit"],
            config.get("voitta_rag_include_folders"),
        )
    )

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        parsed = None

    if isinstance(parsed, list):
        rendered = []
        for item in parsed:
            path = item.get("file_path") or item.get("path") or "unknown"
            text = item.get("text") or item.get("content") or ""
            score = item.get("score")
            rendered.append(
                "===== CHUNK: {0} (score {1}) =====\n{2}\n".format(path, score, text)
            )
        body = "".join(rendered)
    else:
        body = raw

    retval = "voitta-rag retrieved context:\n\n" + body
    return retval


BUILDERS = {
    "baseline": build_baseline,
    "llm-tldr": build_llm_tldr,
    "voitta-rag": build_voitta_rag,
}
