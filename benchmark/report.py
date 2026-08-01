"""Aggregate a scored run into a mode x question-class table.

Usage:
    python3 report.py --scored results/runs-20260726T101500Z-scored.jsonl
"""

import argparse
import json
from collections import defaultdict


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scored", required=True)
    args = parser.parse_args()

    records = []
    with open(args.scored) as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    by_mode = defaultdict(list)
    dropped = 0
    for record in records:
        if "score_total" not in record:
            dropped += 1
            continue
        by_mode[record["mode"]].append(record)

    print("mode          n   score/12   tokens_in   tokens_out   sec    cost_usd")
    print("-" * 72)
    for mode in sorted(by_mode):
        rows = by_mode[mode]
        count = len(rows)
        print(
            "{0:<12} {1:>3}   {2:>7.2f}   {3:>9.0f}   {4:>10.0f}   {5:>5.1f}   {6:>8.4f}".format(
                mode,
                count,
                sum(r["score_total"] for r in rows) / count,
                sum(r["tokens_in"] for r in rows) / count,
                sum(r["tokens_out"] for r in rows) / count,
                sum(r["wall_time_s"] for r in rows) / count,
                sum(r["cost_usd"] or 0 for r in rows) / count,
            )
        )

    print("\nby question class (mean score/12)")
    print("-" * 72)
    classes = sorted({r["question_class"] for r in records if "score_total" in r})
    header = "class".ljust(26) + "".join(m.ljust(14) for m in sorted(by_mode))
    print(header)
    for question_class in classes:
        row = question_class.ljust(26)
        for mode in sorted(by_mode):
            cells = [
                r["score_total"]
                for r in by_mode[mode]
                if r["question_class"] == question_class
            ]
            value = "{0:.2f}".format(sum(cells) / len(cells)) if cells else "-"
            row += value.ljust(14)
        print(row)

    if dropped:
        print(
            "\nNOTE: {0} record(s) excluded from all figures above "
            "(errored or unscored).".format(dropped)
        )


if __name__ == "__main__":
    main()
