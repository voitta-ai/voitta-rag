"""Context builders, one per benchmark mode.

Each builder takes (config, repo_root, question) and returns the context string
that gets injected into the answering prompt. Adding a mode means adding one
function here and one entry in BUILDERS -- nothing else in the harness changes.

The exception is AGENTIC_MODES at the bottom: those answer by giving the model
read-only repo tools instead of an injected context string, so they are a
different interaction shape and runner.py dispatches them separately.
"""

import asyncio
import fnmatch
import json
import os
import re
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
    """Raw source dump, packed in path order up to a character budget.

    The budget is a packing heuristic only. Real token counts come from the
    API usage field on the answering call, never from an offline estimate.

    Packing stops at the first file that does not fit rather than skipping it
    and continuing. Skip-and-continue silently biases the dump against large
    files -- at a 600000-char budget it admitted 69 of jsoup's 97 files while
    dropping Parser.java, Tokeniser.java, and TreeBuilder.java, so the mode
    measured "the small files" while reporting itself as the full-repo control.
    Truncating at a prefix is still lossy, but it is lossy in a way the header
    states honestly. Size the budget so nothing truncates.
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
            break
        chunks.append(block)
        used += len(block)
        included += 1

    if included == len(paths):
        header = (
            "Repository source dump. All {0} matching files included.\n\n".format(
                len(paths)
            )
        )
    else:
        header = (
            "Repository source dump. TRUNCATED: the first {0} of {1} matching "
            "files in path order, cut off by a {2}-character budget. Files after "
            "'{3}' are absent from this dump but do exist in the repository.\n\n".format(
                included, len(paths), budget, paths[included - 1]
            )
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


def build_llm_tldr_structural(config, repo_root, question):
    """Structural context from llm-tldr, via `extract` rather than `semantic search`.

    The `llm-tldr` mode above scores near-zero on citation accuracy because
    `tldr semantic search` reports `"line": 1` for every code unit, leaving the
    answering model no real line numbers to cite. This adapter exists to separate
    "llm-tldr cannot produce citable context" from "that one subcommand cannot".

    `tldr extract` does carry real `line_number` fields for classes and methods,
    but it is per-file and takes no query, so semantic search is still used --
    only to rank which files are relevant. The line numbers then come from
    `extract`. (`tldr structure` was the other candidate and is not usable here:
    it emits no line numbers at all, and parses 50 of jsoup's 97 files.)
    """
    tldr_bin = config["tldr_bin"]
    max_files = config.get("tldr_structural_max_files", 8)

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

    ranked = _ranked_files_from_search(search.stdout, repo_root)
    if not ranked:
        raise RuntimeError(
            "tldr semantic search returned no resolvable file paths; "
            "the semantic index is probably empty (see prepare_llm_tldr)"
        )

    sections = []
    for relative in ranked[:max_files]:
        extract = subprocess.run(
            [tldr_bin, "extract", relative],
            capture_output=True,
            text=True,
            cwd=repo_root,
        )
        if extract.returncode != 0:
            continue
        sections.append(
            "===== EXTRACT: {0} =====\n{1}\n".format(relative, extract.stdout)
        )

    if not sections:
        raise RuntimeError("tldr extract produced no output for any ranked file")

    header = (
        "llm-tldr structural context. Files ranked by semantic search, then "
        "analysed with `tldr extract`; `line_number` fields are real source "
        "lines.\n\n"
    )
    retval = header + "".join(sections)
    return retval


def _ranked_files_from_search(stdout, repo_root):
    """Repo-relative file paths from `tldr semantic search` output, best first.

    The CLI prints human-readable text rather than JSON, so paths are recovered
    by scanning for anything that looks like a path into the repo and keeping
    first-seen order (which is rank order). Deduplicated because several ranked
    code units usually share a file.
    """
    seen = []
    for match in re.finditer(r"[\w./-]+\.\w+", stdout):
        candidate = match.group(0)
        absolute = os.path.join(repo_root, candidate)
        if not os.path.isfile(absolute):
            continue
        if candidate in seen:
            continue
        seen.append(candidate)
    retval = seen
    return retval


def build_repomix(config, repo_root, question):
    """Whole-repo pack produced by the Repomix CLI.

    Repomix is included because hq#88 lists it as an input-side tokens-in tool
    claiming ~70% token reduction. Note what that claim is actually about: the
    reduction comes from *file selection* (respecting .gitignore, dropping
    binaries), not from compressing the text of the files it keeps. Pointed at
    the same include/exclude globs as `baseline`, it emits the same source with
    different framing.
    """
    include = ",".join(config["repo"]["include_globs"])
    ignore = ",".join(config["repo"].get("exclude_globs", []))
    output_path = os.path.join(
        config.get("repomix_tmp_dir", "/tmp"),
        "repomix-{0}.txt".format(config["repo"]["id"]),
    )

    command = [
        config.get("repomix_bin", "npx"),
        "--yes",
        "repomix",
        repo_root,
        "--include",
        include,
        "--output",
        output_path,
        "--style",
        config.get("repomix_style", "plain"),
    ]
    if ignore:
        command[len(command) - 4:len(command) - 4] = ["--ignore", ignore]

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            "repomix failed ({0}): {1}".format(result.returncode, result.stderr.strip())
        )

    with open(output_path, "r", errors="replace") as handle:
        retval = handle.read()
    return retval


def build_caveman_compression(config, repo_root, question):
    """`baseline` dump passed through wilpel/caveman-compression.

    hq#88 places this on the tokens-in axis, applied to a baseline context dump.
    The rule-based (spaCy) variant is used rather than the default one, which
    calls out to a second LLM -- a non-deterministic compressor inside a
    benchmark cell would make the cell unattributable, and it would put a
    different vendor's model in the measurement path.

    The tool is designed for prose: it strips articles, connectives, and other
    grammar an LLM can reconstruct. Source code is not prose, so read this arm as
    a test of that mismatch rather than as the tool used as intended.
    """
    source = build_baseline(config, repo_root, question)
    retval = _caveman_compress(config, source)
    return retval


def _caveman_compress(config, text):
    """Run the rule-based compressor over `text`.

    The CLI takes a file and a mode, not stdin, and prints a banner plus stats
    around the payload -- so the input goes to a temp file and the output is
    taken from `-o` rather than from stdout.
    """
    script = os.path.join(config["caveman_compression_dir"], "caveman_compress_nlp.py")
    tmp_dir = config.get("repomix_tmp_dir", "/tmp")
    in_path = os.path.join(tmp_dir, "caveman-in.txt")
    out_path = os.path.join(tmp_dir, "caveman-out.txt")
    # spaCy refuses documents over nlp.max_length (1,000,000 chars by default),
    # and a whole-repo dump is larger than that, so compress in pieces.
    chunk_size = config.get("caveman_chunk_chars", 400000)

    pieces = []
    for start in range(0, len(text), chunk_size):
        with open(in_path, "w") as handle:
            handle.write(text[start:start + chunk_size])

        result = subprocess.run(
            [
                config.get("caveman_python", "python3"),
                script,
                "compress",
                "-f",
                in_path,
                "-o",
                out_path,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "caveman_compress_nlp failed ({0}): {1}".format(
                    result.returncode, result.stderr.strip()[:400]
                )
            )

        with open(out_path, "r", errors="replace") as handle:
            pieces.append(handle.read())

    retval = "".join(pieces)
    return retval


def build_repomix_caveman(config, repo_root, question):
    """Repomix pack, then caveman-compression over it (hq#88 cell 7)."""
    retval = _caveman_compress(config, build_repomix(config, repo_root, question))
    return retval


def build_llm_tldr_then_voitta_rag(config, repo_root, question):
    """Chain from hq#75: llm-tldr structure first, then RAG retrieval.

    The premise in the plan is that structural context tells the model *where*
    to look and retrieval supplies the surrounding source, so the two compose
    rather than compete. Both halves are the same builders used standalone, so
    any difference is attributable to the composition.
    """
    structural = build_llm_tldr(config, repo_root, question)
    retrieved = build_voitta_rag(config, repo_root, question)
    retval = "{0}\n\n{1}".format(structural, retrieved)
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


def _expand_index_folders(spec):
    """Every folder path under an index root, as voitta-rag stores them.

    `include_folders` over MCP is an **exact** match on a chunk's `folder_path`,
    and `folder_path` is the directory the file sits in -- not the index root.
    The subtree-prefix expansion in `mcp_server.search` only runs when a
    `user_name` is supplied, and the MCP tool signature has no such parameter, so
    over MCP there is no subtree scoping at all.

    Passing `["jsoup"]` therefore does not scope to the jsoup repo. It scopes to
    files sitting *directly at* the repo root -- for jsoup that is CHANGES.md,
    change-archive.txt, README.md, LICENSE, and SECURITY.md, and nothing under
    src/. The 2026-07-31 run of this benchmark did exactly that and concluded
    that retrieval "ranked changelogs above the source"; it had in fact been
    handed a five-file changelog corpus and had no source to rank. See the
    README.

    So enumerate the directories locally and pass all of them.
    """
    root = os.path.expanduser(spec["local_dir"])
    name = spec["name"]
    folders = set()
    for relative in _iter_repo_files(
        root, spec.get("include_globs", ["**/*"]), spec.get("exclude_globs", [])
    ):
        directory = os.path.dirname(relative)
        folders.add("{0}/{1}".format(name, directory) if directory else name)
    retval = sorted(folders)
    return retval


def build_voitta_rag_java(config, repo_root, question):
    """voitta-rag restricted to an index of exactly the benchmark's file set.

    The `voitta-rag` mode indexes the repository as checked out, which for jsoup
    means 233 files including CHANGES.md and change-archive.txt, while `baseline`
    and `llm-tldr` see 97 .java files. That asymmetry is not incidental: jsoup's
    changelog describes parser behaviour in the same prose vocabulary the
    questions use, so it outranks the source. This mode indexes only the 97
    files, so retrieval quality is measured rather than corpus choice.
    """
    retval = build_voitta_rag(
        config, repo_root, question, index=config["voitta_rag_java_index"]
    )
    return retval


def build_voitta_rag(config, repo_root, question, index=None):
    """Retrieved chunks from a running voitta-rag instance over MCP."""
    folders = _expand_index_folders(index or config["voitta_rag_index"])
    raw = asyncio.run(
        _voitta_rag_search(
            config["voitta_rag_mcp_url"],
            question["text"],
            config["voitta_rag_search_limit"],
            folders,
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
    "llm-tldr-structural": build_llm_tldr_structural,
    "voitta-rag": build_voitta_rag,
    "voitta-rag-java": build_voitta_rag_java,
    "repomix": build_repomix,
    "caveman-compression": build_caveman_compression,
    "repomix-caveman": build_repomix_caveman,
    "llm-tldr-then-voitta-rag": build_llm_tldr_then_voitta_rag,
}

# Modes answered by an agentic tool loop rather than an injected context string.
# `cce` is Claude Code's native exploration (Read/Grep/Glob), which is a
# different interaction shape, not a different context builder -- there is no
# context to inject, so runner.py dispatches these separately. `llm-tldr-then-cce`
# seeds that loop with llm-tldr structural context, which is the second chain
# mode in hq#75.
AGENTIC_MODES = {
    "cce": None,
    "llm-tldr-then-cce": build_llm_tldr,
}
