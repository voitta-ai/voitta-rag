"""Benchmark driver: run every (question, mode) pair and log one JSONL record each.

The prompt template is identical across modes. Only the injected context differs,
so any measured difference is attributable to the context strategy under test.

Usage:
    python3 runner.py --config config.json --questions questions.jsonl
    python3 runner.py --modes voitta-rag --questions questions.jsonl
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone

import anthropic

import modes

PROMPT_TEMPLATE = """You are answering a question about the `{repo_id}` codebase.

Everything you know about this repository is in the CONTEXT block below. Do not
rely on prior knowledge of this project; if the context does not support a claim,
say so rather than guessing.

Every factual claim you make must carry a `file:line` citation pointing at the
source that supports it. A claim without a citation will be scored as unsupported.

<context>
{context}
</context>

<question>
{question}
</question>
"""


def load_config(path):
    with open(path) as handle:
        retval = json.load(handle)
    return retval


def load_questions(path):
    questions = []
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if line:
                questions.append(json.loads(line))
    return questions


def compute_cost(config, model, usage):
    rates = config["pricing_usd_per_mtok"].get(model)
    if not rates:
        return None
    cost = (
        usage.input_tokens * rates["input"] + usage.output_tokens * rates["output"]
    ) / 1_000_000
    retval = round(cost, 6)
    return retval


def answer_one(client, config, repo_root, question, mode):
    builder = modes.BUILDERS[mode]

    context_start = time.monotonic()
    context = builder(config, repo_root, question)
    context_seconds = time.monotonic() - context_start

    prompt = PROMPT_TEMPLATE.format(
        repo_id=config["repo"]["id"],
        context=context,
        question=question["text"],
    )

    call_start = time.monotonic()
    with client.messages.stream(
        model=config["answer_model"],
        max_tokens=config["answer_max_tokens"],
        thinking={"type": "adaptive"},
        output_config={"effort": config["answer_effort"]},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        response = stream.get_final_message()
    call_seconds = time.monotonic() - call_start

    if response.stop_reason == "refusal":
        text = ""
    else:
        text = "\n".join(
            block.text for block in response.content if block.type == "text"
        )

    record = {
        "repo": config["repo"]["id"],
        "question_id": question["id"],
        "question_class": question["class"],
        "mode": mode,
        "model": config["answer_model"],
        "effort": config["answer_effort"],
        "context_chars": len(context),
        "context_build_seconds": round(context_seconds, 3),
        "tokens_in": response.usage.input_tokens,
        "tokens_out": response.usage.output_tokens,
        "wall_time_s": round(call_seconds, 3),
        "cost_usd": compute_cost(config, config["answer_model"], response.usage),
        "stop_reason": response.stop_reason,
        "raw_output": text,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
    return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--questions", default="questions.jsonl")
    parser.add_argument("--modes", nargs="*", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="rebuild per-mode indexes before running (llm-tldr warm + semantic index)",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    questions = load_questions(args.questions)
    selected_modes = args.modes or config["modes"]

    repo_root = config["repo"]["path"]
    if not os.path.isdir(repo_root):
        raise SystemExit("repo path does not exist: {0}".format(repo_root))

    output_dir = config["output_dir"]
    os.makedirs(output_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = args.out or os.path.join(output_dir, "runs-{0}.jsonl".format(stamp))

    if args.prepare and "llm-tldr" in selected_modes:
        print("preparing llm-tldr indexes...")
        modes.prepare_llm_tldr(config, repo_root)

    client = anthropic.Anthropic()

    total = len(questions) * len(selected_modes)
    done = 0
    with open(out_path, "a") as handle:
        for question in questions:
            for mode in selected_modes:
                done += 1
                label = "[{0}/{1}] {2} / {3}".format(
                    done, total, question["id"], mode
                )
                try:
                    record = answer_one(client, config, repo_root, question, mode)
                except Exception as error:
                    record = {
                        "repo": config["repo"]["id"],
                        "question_id": question["id"],
                        "question_class": question["class"],
                        "mode": mode,
                        "error": "{0}: {1}".format(type(error).__name__, error),
                        "ran_at": datetime.now(timezone.utc).isoformat(),
                    }
                    print("{0} FAILED: {1}".format(label, record["error"]))
                else:
                    print(
                        "{0} in={1} out={2} {3}s ${4}".format(
                            label,
                            record["tokens_in"],
                            record["tokens_out"],
                            record["wall_time_s"],
                            record["cost_usd"],
                        )
                    )
                handle.write(json.dumps(record) + "\n")
                handle.flush()

    print("\nwrote {0}".format(out_path))


if __name__ == "__main__":
    main()
