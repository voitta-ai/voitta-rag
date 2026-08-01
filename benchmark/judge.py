"""Judge pass: score each answer against the repo it claims to describe.

The judge gets read-only tools over the repo under test, so every `file:line`
citation in an answer can be checked against the actual source rather than
taken on trust.

Usage:
    python3 judge.py --runs results/runs-20260726T101500Z.jsonl
"""

import argparse
import fnmatch
import json
import os
import subprocess
from datetime import datetime, timezone

import anthropic

JUDGE_SYSTEM = """You are grading an answer about a codebase you can read directly.

Use the read_file, grep, and glob tools to verify the answer against the real
source. Check every `file:line` citation the answer makes: resolve it, read the
surrounding code, and decide whether it actually supports the claim it is attached
to. Do not accept a citation because it looks plausible.

Score four dimensions, 0-3 each:
  correctness       - the claims are true given the repository
  completeness      - the answer covers what the question asked
  citation_accuracy - citations resolve and support the claims they are attached to
  conciseness       - the answer is free of padding and irrelevant material

Investigate before scoring. Report every problem you find, including ones you are
uncertain about or consider minor -- a later pass filters for importance, so
coverage matters more than selectivity here. When you are done investigating,
return your scores in the required JSON format.
"""

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "correctness": {"type": "integer", "enum": [0, 1, 2, 3]},
        "completeness": {"type": "integer", "enum": [0, 1, 2, 3]},
        "citation_accuracy": {"type": "integer", "enum": [0, 1, 2, 3]},
        "conciseness": {"type": "integer", "enum": [0, 1, 2, 3]},
        "verified_citation_count": {"type": "integer"},
        "bogus_citation_count": {"type": "integer"},
        "judge_notes": {"type": "string"},
    },
    "required": [
        "correctness",
        "completeness",
        "citation_accuracy",
        "conciseness",
        "verified_citation_count",
        "bogus_citation_count",
        "judge_notes",
    ],
    "additionalProperties": False,
}

TOOLS = [
    {
        "name": "read_file",
        "description": (
            "Read a file from the repository under test. Returns the file with "
            "1-based line numbers prefixed, so citations can be checked directly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repo-relative path, e.g. src/main/java/org/jsoup/Jsoup.java",
                },
                "start_line": {"type": "integer", "description": "1-based first line"},
                "end_line": {"type": "integer", "description": "1-based last line"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "grep",
        "description": "Search the repository for a regular expression. Returns path:line:text matches.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path_glob": {
                    "type": "string",
                    "description": "Optional glob to restrict the search, e.g. **/*.java",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "glob",
        "description": "List repository files matching a glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    },
]

MAX_TOOL_OUTPUT_CHARS = 40000


def _safe_path(repo_root, relative):
    """Resolve a model-supplied path and confine it to the repository root."""
    root = os.path.realpath(repo_root)
    candidate = os.path.realpath(os.path.join(root, relative))
    if candidate != root and not candidate.startswith(root + os.sep):
        raise ValueError("path escapes the repository root: {0}".format(relative))
    return candidate


def _truncate(text):
    if len(text) <= MAX_TOOL_OUTPUT_CHARS:
        return text
    retval = text[:MAX_TOOL_OUTPUT_CHARS] + "\n[output truncated]"
    return retval


def tool_read_file(repo_root, tool_input):
    path = _safe_path(repo_root, tool_input["path"])
    with open(path, "r", errors="replace") as handle:
        lines = handle.readlines()

    start = tool_input.get("start_line") or 1
    end = tool_input.get("end_line") or len(lines)
    start = max(1, start)
    end = min(len(lines), end)

    numbered = []
    for number in range(start, end + 1):
        numbered.append("{0}: {1}".format(number, lines[number - 1].rstrip("\n")))
    retval = _truncate("\n".join(numbered))
    return retval


def tool_grep(repo_root, tool_input):
    command = ["grep", "-rnI", "-E", tool_input["pattern"]]
    path_glob = tool_input.get("path_glob")
    if path_glob:
        command.extend(["--include", os.path.basename(path_glob)])
    command.append(".")

    result = subprocess.run(
        command, cwd=repo_root, capture_output=True, text=True, timeout=60
    )
    if result.returncode not in (0, 1):
        retval = "grep failed: {0}".format(result.stderr.strip())
    else:
        retval = _truncate(result.stdout) or "no matches"
    return retval


def tool_glob(repo_root, tool_input):
    pattern = tool_input["pattern"]
    matches = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for filename in filenames:
            relative = os.path.relpath(os.path.join(dirpath, filename), repo_root)
            if fnmatch.fnmatch(relative, pattern):
                matches.append(relative)
    matches.sort()
    retval = _truncate("\n".join(matches)) or "no matches"
    return retval


HANDLERS = {"read_file": tool_read_file, "grep": tool_grep, "glob": tool_glob}


def run_tool(repo_root, name, tool_input):
    handler = HANDLERS.get(name)
    if handler is None:
        return "unknown tool: {0}".format(name), True
    try:
        output = handler(repo_root, tool_input)
    except Exception as error:
        return "{0}: {1}".format(type(error).__name__, error), True
    return output, False


def judge_one(client, config, repo_root, record):
    prompt = (
        "<question>\n{0}\n</question>\n\n"
        "<answer_under_review>\n{1}\n</answer_under_review>\n\n"
        "Verify the answer against the repository, then return the scores."
    ).format(record["question_text"], record["raw_output"])

    messages: list = [{"role": "user", "content": prompt}]
    iterations = 0

    while True:
        iterations += 1
        if iterations > config["judge_max_iterations"]:
            raise RuntimeError("judge exceeded max iterations")

        response = client.messages.create(
            model=config["judge_model"],
            max_tokens=config["judge_max_tokens"],
            system=JUDGE_SYSTEM,
            thinking={"type": "adaptive"},
            output_config={
                "effort": config["judge_effort"],
                "format": {"type": "json_schema", "schema": SCORE_SCHEMA},
            },
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "refusal":
            raise RuntimeError("judge refused: {0}".format(response.stop_details))

        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            text = "\n".join(
                block.text for block in response.content if block.type == "text"
            )
            scores = json.loads(text)
            scores["judge_iterations"] = iterations
            scores["judge_tokens_in"] = response.usage.input_tokens
            scores["judge_tokens_out"] = response.usage.output_tokens
            return scores

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in tool_uses:
            output, is_error = run_tool(repo_root, block.name, block.input)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                    "is_error": is_error,
                }
            )
        messages.append({"role": "user", "content": results})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--questions", default="questions.jsonl")
    parser.add_argument("--runs", required=True)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    with open(args.config) as handle:
        config = json.load(handle)

    question_text = {}
    with open(args.questions) as handle:
        for line in handle:
            line = line.strip()
            if line:
                question = json.loads(line)
                question_text[question["id"]] = question["text"]

    repo_root = config["repo"]["path"]
    client = anthropic.Anthropic()
    out_path = args.out or args.runs.replace(".jsonl", "-scored.jsonl")

    records = []
    with open(args.runs) as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    with open(out_path, "a") as handle:
        for index, record in enumerate(records, start=1):
            label = "[{0}/{1}] {2} / {3}".format(
                index, len(records), record.get("question_id"), record.get("mode")
            )
            if record.get("error") or not record.get("raw_output"):
                print("{0} skipped (no answer to score)".format(label))
                handle.write(json.dumps(record) + "\n")
                continue

            record["question_text"] = question_text.get(record["question_id"], "")
            try:
                scores = judge_one(client, config, repo_root, record)
            except Exception as error:
                record["judge_error"] = "{0}: {1}".format(type(error).__name__, error)
                print("{0} JUDGE FAILED: {1}".format(label, record["judge_error"]))
            else:
                record["scores"] = scores
                record["score_total"] = (
                    scores["correctness"]
                    + scores["completeness"]
                    + scores["citation_accuracy"]
                    + scores["conciseness"]
                )
                print(
                    "{0} total={1}/12 (bogus citations: {2})".format(
                        label, record["score_total"], scores["bogus_citation_count"]
                    )
                )
            record["scored_at"] = datetime.now(timezone.utc).isoformat()
            handle.write(json.dumps(record) + "\n")
            handle.flush()

    print("\nwrote {0}".format(out_path))


if __name__ == "__main__":
    main()
