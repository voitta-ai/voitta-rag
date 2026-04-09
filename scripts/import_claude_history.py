#!/usr/bin/env python3
"""Import Claude Code session history as voitta-rag memories.

Parses ~/.claude/history.jsonl, groups user prompts by sessionId,
and creates one memory per session via the voitta-rag MCP HTTP API.

Usage:
    python3 scripts/import_claude_history.py [options]

Options:
    --voitta-url URL     Base URL of voitta-rag (default: http://localhost:8000)
    --user NAME          User name for X-User-Name header (default: current OS user)
    --history PATH       Path to history.jsonl (default: ~/.claude/history.jsonl)
    --project FILTER     Only import sessions from this project/directory (substring match)
    --after DATE         Only import sessions after this date (YYYY-MM-DD)
    --before DATE        Only import sessions before this date (YYYY-MM-DD)
    --keyword WORD       Only import sessions containing this keyword in prompts
    --dry-run            Show what would be imported without creating memories
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests


def parse_history(history_path: Path) -> list[dict]:
    """Parse history.jsonl into a list of prompt records."""
    entries = []
    with open(history_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                entries.append(entry)
            except json.JSONDecodeError:
                print(f"  Warning: skipping malformed line {line_num}", file=sys.stderr)
    return entries


def group_by_session(entries: list[dict]) -> dict[str, list[dict]]:
    """Group history entries by sessionId, sorted by timestamp within each session."""
    sessions = defaultdict(list)
    for entry in entries:
        sid = entry.get("sessionId")
        if not sid:
            continue
        sessions[sid].append(entry)
    for sid in sessions:
        sessions[sid].sort(key=lambda e: e.get("timestamp", 0))
    return dict(sessions)


def session_date_range(prompts: list[dict]) -> tuple[datetime, datetime]:
    """Return (earliest, latest) datetime from a list of prompts."""
    timestamps = [p["timestamp"] for p in prompts if "timestamp" in p]
    earliest = datetime.fromtimestamp(min(timestamps) / 1000, tz=timezone.utc)
    latest = datetime.fromtimestamp(max(timestamps) / 1000, tz=timezone.utc)
    return earliest, latest


def format_session_memory(session_id: str, prompts: list[dict]) -> str:
    """Format a session's prompts into memory content."""
    earliest, latest = session_date_range(prompts)
    projects = set()
    for p in prompts:
        proj = p.get("project", "")
        if proj:
            projects.add(proj)

    lines = []
    lines.append(f"# Claude Code Session")
    lines.append(f"")
    lines.append(f"**Session ID:** {session_id}")
    lines.append(f"**Date:** {earliest.strftime('%Y-%m-%d %H:%M')} - {latest.strftime('%Y-%m-%d %H:%M')} UTC")
    if projects:
        lines.append(f"**Project(s):** {', '.join(sorted(projects))}")
    lines.append(f"**Prompts:** {len(prompts)}")
    lines.append(f"")
    lines.append(f"## User Prompts")
    lines.append(f"")

    for i, p in enumerate(prompts, 1):
        ts = datetime.fromtimestamp(p["timestamp"] / 1000, tz=timezone.utc)
        display = p.get("display", "")
        lines.append(f"### Prompt {i} ({ts.strftime('%H:%M:%S')})")
        lines.append(f"")
        lines.append(display)
        lines.append(f"")

    result = "\n".join(lines)
    return result


def matches_filters(
    prompts: list[dict],
    project_filter: str | None,
    after_date: datetime | None,
    before_date: datetime | None,
    keyword: str | None,
) -> bool:
    """Check whether a session matches all provided filters."""
    if not prompts:
        return False

    earliest, latest = session_date_range(prompts)

    if after_date and latest < after_date:
        return False
    if before_date and earliest > before_date:
        return False

    if project_filter:
        session_projects = [p.get("project", "") for p in prompts]
        found = any(project_filter in proj for proj in session_projects)
        if not found:
            return False

    if keyword:
        kw_lower = keyword.lower()
        all_text = " ".join(p.get("display", "") for p in prompts).lower()
        if kw_lower not in all_text:
            return False

    return True


def create_memory(voitta_url: str, user_name: str, content: str) -> dict:
    """Create a memory via the voitta-rag MCP endpoint."""
    url = f"{voitta_url}/mcp/mcp"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "create_memory",
            "arguments": {"content": content},
        },
    }
    headers = {
        "Content-Type": "application/json",
        "X-User-Name": user_name,
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Import Claude Code session history as voitta-rag memories"
    )
    parser.add_argument(
        "--voitta-url",
        default=os.getenv("VOITTA_URL", "http://localhost:8000"),
        help="Base URL of voitta-rag (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--user",
        default=os.getenv("USER", "anonymous"),
        help="User name for X-User-Name header",
    )
    parser.add_argument(
        "--history",
        default=str(Path.home() / ".claude" / "history.jsonl"),
        help="Path to history.jsonl",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="Only import sessions from this project/directory (substring match)",
    )
    parser.add_argument(
        "--after",
        default=None,
        help="Only import sessions after this date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--before",
        default=None,
        help="Only import sessions before this date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--keyword",
        default=None,
        help="Only import sessions containing this keyword in prompts",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be imported without creating memories",
    )

    args = parser.parse_args()

    history_path = Path(args.history)
    if not history_path.exists():
        print(f"Error: history file not found: {history_path}", file=sys.stderr)
        sys.exit(1)

    after_date = None
    if args.after:
        after_date = datetime.strptime(args.after, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    before_date = None
    if args.before:
        before_date = datetime.strptime(args.before, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    print(f"Reading {history_path}...")
    entries = parse_history(history_path)
    print(f"  Found {len(entries)} prompt entries")

    sessions = group_by_session(entries)
    print(f"  Found {len(sessions)} sessions")

    matched = 0
    imported = 0
    for session_id, prompts in sorted(sessions.items(), key=lambda kv: min(p.get("timestamp", 0) for p in kv[1])):
        if not matches_filters(prompts, args.project, after_date, before_date, args.keyword):
            continue
        matched += 1

        earliest, latest = session_date_range(prompts)
        projects = set(p.get("project", "") for p in prompts if p.get("project"))
        proj_str = ", ".join(sorted(projects)) if projects else "(none)"

        print(f"\n  Session {session_id[:8]}...")
        print(f"    Date: {earliest.strftime('%Y-%m-%d %H:%M')} - {latest.strftime('%H:%M')} UTC")
        print(f"    Project: {proj_str}")
        print(f"    Prompts: {len(prompts)}")

        if args.dry_run:
            print(f"    [dry-run] Would create memory")
            continue

        content = format_session_memory(session_id, prompts)
        try:
            result = create_memory(args.voitta_url, args.user, content)
            error = result.get("error")
            if error:
                print(f"    Error: {error}", file=sys.stderr)
            else:
                imported += 1
                print(f"    Created memory")
        except Exception as e:
            print(f"    Error creating memory: {e}", file=sys.stderr)

    print(f"\nDone. Matched: {matched}, Imported: {imported}")


if __name__ == "__main__":
    main()
