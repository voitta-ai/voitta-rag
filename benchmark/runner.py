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

import judge as judge_tools
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

# The agentic counterpart. Deliberately the same task, the same citation
# requirement, and the same closing question block as PROMPT_TEMPLATE -- only the
# means of reaching the source differs, so the comparison stays attributable.
AGENTIC_PROMPT_TEMPLATE = """You are answering a question about the `{repo_id}` codebase.

You have read-only tools over the repository: `read_file`, `grep`, and `glob`.
Explore as much as you need before answering. Do not rely on prior knowledge of
this project; if you cannot find support in the repository, say so rather than
guessing.

Every factual claim you make must carry a `file:line` citation pointing at the
source that supports it. A claim without a citation will be scored as unsupported.
{seed}
<question>
{question}
</question>
"""

# hq#88's output-side axis: same input context, but the model is asked to answer
# in compressed style. Orthogonal to every tokens-in mode, so it rides on top of
# any of them rather than being a mode of its own.
CAVEMAN_OUTPUT_OVERLAY = """
Answer in compressed "caveman" style: drop articles, filler, and hedging;
fragments are fine; use short synonyms. Do not drop technical substance --
every `file:line` citation, symbol name, and factual claim must survive intact.
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


def answer_text(response):
    if response.stop_reason == "refusal":
        retval = ""
    else:
        retval = "\n".join(
            block.text for block in response.content if block.type == "text"
        )
    return retval


def answer_injected(client, config, prompt):
    """One-shot answer over an injected context string."""
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

    retval = {
        "tokens_in": response.usage.input_tokens,
        "tokens_out": response.usage.output_tokens,
        "cost_usd": compute_cost(config, config["answer_model"], response.usage),
        "stop_reason": response.stop_reason,
        "raw_output": answer_text(response),
        "wall_time_s": round(call_seconds, 3),
        "tool_calls": 0,
    }
    return retval


def answer_agentic(client, config, repo_root, prompt):
    """Answer by exploring the repo with read-only tools instead of injected context.

    Tokens are summed across every turn of the loop, so `tokens_in` for an
    agentic mode is cumulative and is not comparable to a one-shot mode's
    single-request figure without saying so. That is the honest number: it is
    what the strategy actually costs to answer one question.
    """
    messages = [{"role": "user", "content": prompt}]
    tokens_in = 0
    tokens_out = 0
    cost = 0.0
    tool_calls = 0
    stop_reason = None
    response = None

    call_start = time.monotonic()
    for _ in range(config.get("cce_max_iterations", 40)):
        with client.messages.stream(
            model=config["answer_model"],
            max_tokens=config["answer_max_tokens"],
            thinking={"type": "adaptive"},
            output_config={"effort": config["answer_effort"]},
            tools=judge_tools.TOOLS,
            messages=messages,
        ) as stream:
            response = stream.get_final_message()

        tokens_in += response.usage.input_tokens
        tokens_out += response.usage.output_tokens
        cost += compute_cost(config, config["answer_model"], response.usage) or 0.0
        stop_reason = response.stop_reason

        if response.stop_reason != "tool_use":
            break

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            tool_calls += 1
            output, is_error = judge_tools.run_tool(
                repo_root, block.name, block.input
            )
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                    "is_error": is_error,
                }
            )
        messages.append({"role": "user", "content": results})
    call_seconds = time.monotonic() - call_start

    retval = {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": round(cost, 6),
        "stop_reason": stop_reason,
        "raw_output": answer_text(response) if response else "",
        "wall_time_s": round(call_seconds, 3),
        "tool_calls": tool_calls,
    }
    return retval


def answer_one(client, config, repo_root, question, mode, caveman_output=False):
    agentic = mode in modes.AGENTIC_MODES

    context_start = time.monotonic()
    if agentic:
        seed_builder = modes.AGENTIC_MODES[mode]
        context = seed_builder(config, repo_root, question) if seed_builder else ""
    else:
        context = modes.BUILDERS[mode](config, repo_root, question)
    context_seconds = time.monotonic() - context_start

    if agentic:
        seed = ""
        if context:
            seed = (
                "\nA structural summary of the repository is below to start you "
                "off. It may be incomplete; verify against the real files before "
                "citing them.\n\n<context>\n{0}\n</context>\n".format(context)
            )
        prompt = AGENTIC_PROMPT_TEMPLATE.format(
            repo_id=config["repo"]["id"], seed=seed, question=question["text"]
        )
    else:
        prompt = PROMPT_TEMPLATE.format(
            repo_id=config["repo"]["id"],
            context=context,
            question=question["text"],
        )

    if caveman_output:
        prompt = prompt + CAVEMAN_OUTPUT_OVERLAY

    if agentic:
        outcome = answer_agentic(client, config, repo_root, prompt)
    else:
        outcome = answer_injected(client, config, prompt)

    record = {
        "repo": config["repo"]["id"],
        "question_id": question["id"],
        "question_class": question["class"],
        "mode": mode,
        "output_style": "caveman" if caveman_output else "default",
        "model": config["answer_model"],
        "effort": config["answer_effort"],
        "context_chars": len(context),
        "context_build_seconds": round(context_seconds, 3),
        "wall_time_s": outcome["wall_time_s"],
        "tokens_in": outcome["tokens_in"],
        "tokens_out": outcome["tokens_out"],
        "cost_usd": outcome["cost_usd"],
        "stop_reason": outcome["stop_reason"],
        "tool_calls": outcome["tool_calls"],
        "raw_output": outcome["raw_output"],
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
    parser.add_argument(
        "--caveman-output",
        action="store_true",
        help=(
            "apply the hq#88 output-side compression overlay; orthogonal to the "
            "mode, so any tokens-in mode can be run in both output styles"
        ),
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
                label = "[{0}/{1}] {2} / {3}{4}".format(
                    done,
                    total,
                    question["id"],
                    mode,
                    " (caveman-out)" if args.caveman_output else "",
                )
                try:
                    record = answer_one(
                        client,
                        config,
                        repo_root,
                        question,
                        mode,
                        caveman_output=args.caveman_output,
                    )
                except Exception as error:
                    record = {
                        "repo": config["repo"]["id"],
                        "question_id": question["id"],
                        "question_class": question["class"],
                        "mode": mode,
                        "output_style": "caveman" if args.caveman_output else "default",
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
