"""Aggregate scored runs into mode x question-class tables.

Accepts several scored files so arms run at different times can be reported
together. A mode is keyed by (mode, output_style), because hq#88's output-side
compression is orthogonal to the tokens-in mode and has to be visible as its own
row rather than averaged into one.

Usage:
    python3 report.py --scored results/a-scored.jsonl results/b-scored.jsonl
"""

import argparse
import json
from collections import defaultdict


def load(paths):
    records = []
    for path in paths:
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    return records


def mode_key(record):
    style = record.get("output_style", "default")
    retval = record["mode"] if style == "default" else "{0} +caveman-out".format(
        record["mode"]
    )
    return retval


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scored", required=True, nargs="+")
    args = parser.parse_args()

    records = load(args.scored)

    by_mode = defaultdict(list)
    dropped = 0
    for record in records:
        if "score_total" not in record:
            dropped += 1
            continue
        by_mode[mode_key(record)].append(record)

    def mean(rows, fn):
        values = [fn(r) for r in rows if fn(r) is not None]
        retval = sum(values) / len(values) if values else 0
        return retval

    width = max(len(m) for m in by_mode) + 2
    print(
        "mode".ljust(width)
        + "  n  score/12  tokens_in  tokens_out   sec   $/q    verified  bogus"
    )
    print("-" * (width + 62))
    for mode in sorted(by_mode, key=lambda m: -mean(by_mode[m], lambda r: r["score_total"])):
        rows = by_mode[mode]
        print(
            "{0}{1:>3}   {2:>6.2f}  {3:>9.0f}  {4:>10.0f} {5:>6.1f} {6:>7.4f} {7:>8.0f} {8:>6.0f}".format(
                mode.ljust(width),
                len(rows),
                mean(rows, lambda r: r["score_total"]),
                mean(rows, lambda r: r["tokens_in"]),
                mean(rows, lambda r: r["tokens_out"]),
                mean(rows, lambda r: r["wall_time_s"]),
                mean(rows, lambda r: r["cost_usd"]),
                sum(r["scores"].get("verified_citation_count", 0) for r in rows),
                sum(r["scores"].get("bogus_citation_count", 0) for r in rows),
            )
        )

    print("\nby question class (mean score/12)")
    modes = sorted(by_mode)
    label_width = 26
    print("class".ljust(label_width) + "".join(m[:17].ljust(19) for m in modes))
    print("-" * (label_width + 19 * len(modes)))
    classes = sorted({r["question_class"] for r in records if "score_total" in r})
    for question_class in classes:
        row = question_class.ljust(label_width)
        for mode in modes:
            cells = [
                r["score_total"]
                for r in by_mode[mode]
                if r["question_class"] == question_class
            ]
            value = "{0:.2f}".format(sum(cells) / len(cells)) if cells else "-"
            row += value.ljust(19)
        print(row)

    answer_cost = sum(r.get("cost_usd") or 0 for r in records)
    judge_cost = sum(
        (r.get("scores") or {}).get("judge_cost_usd") or 0 for r in records
    )
    print(
        "\ntotal cost: ${0:.2f} answering + ${1:.2f} judging = ${2:.2f} "
        "over {3} cells".format(
            answer_cost, judge_cost, answer_cost + judge_cost, len(records)
        )
    )

    if dropped:
        print(
            "NOTE: {0} record(s) excluded from all figures above "
            "(errored or unscored).".format(dropped)
        )


if __name__ == "__main__":
    main()
