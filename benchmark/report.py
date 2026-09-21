"""Aggregate scored runs into mode x question-class tables.

Accepts several scored files so arms run at different times can be reported
together. A mode is keyed by (mode, output_style), because hq#88's output-side
compression is orthogonal to the tokens-in mode and has to be visible as its own
row rather than averaged into one.

Each --scored argument may end in "#mode,mode" to take only those modes (as
printed in the table, e.g. "baseline +caveman-out") from that file, so a
superseded arm in an older file does not double-count.

Usage:
    python3 report.py --scored results/a-scored.jsonl "results/b-scored.jsonl#llm-tldr"
"""

import argparse
import json
from collections import defaultdict


def load(specs):
    records = []
    for spec in specs:
        path, _, selector = spec.partition("#")
        wanted = {m.strip() for m in selector.split(",")} if selector else None
        matched = set()
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                key = mode_key(record)
                if wanted is None or key in wanted:
                    matched.add(key)
                    records.append(record)
        # A selector that matches nothing is a typo or a stale mode name, not an
        # intentional omission: the gates below only ever see filtered records.
        if wanted and wanted - matched:
            raise SystemExit(
                "{0}: selected mode(s) not in file: {1}".format(
                    path, ", ".join(sorted(wanted - matched))
                )
            )
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
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="report even if some records errored or were not scored",
    )
    args = parser.parse_args()

    records = load(args.scored)

    by_mode = defaultdict(list)
    dropped = 0
    for record in records:
        if "score_total" not in record:
            dropped += 1
            continue
        by_mode[mode_key(record)].append(record)

    # Means over a mode that lost cells are biased toward the cells that
    # survived, so an incomplete matrix is an error unless asked for.
    if dropped and not args.allow_incomplete:
        raise SystemExit(
            "{0} record(s) errored or unscored; re-run them, or pass "
            "--allow-incomplete to report anyway.".format(dropped)
        )

    # One scored cell per (mode, question). A duplicate means two runs of the
    # same arm were passed together; averaging them silently changes the row.
    duplicates = []
    for mode, rows in by_mode.items():
        seen = defaultdict(int)
        for row in rows:
            seen[row["question_id"]] += 1
        duplicates += [
            "{0} / {1} x{2}".format(mode, q, n) for q, n in seen.items() if n > 1
        ]
    if duplicates:
        raise SystemExit(
            "duplicate scored cells (select one file per mode with "
            "'path#mode'): " + "; ".join(sorted(duplicates))
        )

    if not args.allow_incomplete:
        expected = {r["question_id"] for rows in by_mode.values() for r in rows}
        short = [
            "{0} missing {1}".format(
                mode, ",".join(sorted(expected - {r["question_id"] for r in rows}))
            )
            for mode, rows in by_mode.items()
            if {r["question_id"] for r in rows} != expected
        ]
        if short:
            raise SystemExit(
                "incomplete matrix: " + "; ".join(sorted(short))
                + " (pass --allow-incomplete to report anyway)"
            )

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

    # Default output style only: the caveman-out rows are the same tokens-in
    # modes re-run, so listing them here would double every column.
    print("\nby question class (mean score/12, default output style only)")
    modes = sorted(m for m in by_mode if not m.endswith("+caveman-out"))
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
