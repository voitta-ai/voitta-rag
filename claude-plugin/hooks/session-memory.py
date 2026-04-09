#!/usr/bin/env python3
"""SessionEnd hook that creates a voitta-rag memory from a Claude Code session.

Reads hook input JSON from stdin, then reads the transcript JSONL file pointed
to by transcript_path. Extracts user prompts and assistant text responses,
formats them as markdown, and POSTs to voitta-rag's create_memory MCP tool.

Configured via env vars (set by setup.sh):
    VOITTA_URL   voitta-rag base URL (default: http://localhost:8000)
    VOITTA_USER  X-User-Name header  (default: $USER)

Failures are logged to stderr but do not fail the hook — we never want to
break the user's session close on a memory save error.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import urllib.request
import urllib.error


def read_hook_input() -> dict:
    """Read the JSON payload Claude Code sends on stdin."""
    raw = sys.stdin.read()
    if not raw:
        return {}
    return json.loads(raw)


def extract_turns(transcript_path: Path) -> list[dict]:
    """Extract user prompts and assistant text responses from a transcript.

    Returns list of {role, text, timestamp} in chronological order.
    Skips tool calls, tool results, and system messages.
    """
    turns = []
    with open(transcript_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = entry.get("type")
            if etype not in ("user", "assistant"):
                continue

            msg = entry.get("message", {})
            content = msg.get("content")
            ts = entry.get("timestamp", "")

            if etype == "user":
                # User content may be a string or a list with text/image parts
                text = _flatten_user_content(content)
                if text:
                    turns.append({"role": "user", "text": text, "timestamp": ts})
            else:  # assistant
                text = _flatten_assistant_content(content)
                if text:
                    turns.append({"role": "assistant", "text": text, "timestamp": ts})

    return turns


def _flatten_user_content(content) -> str:
    """User messages: content is either a string or list of parts. Skip tool_result."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(p for p in parts if p).strip()
    return ""


def _flatten_assistant_content(content) -> str:
    """Assistant messages: extract text blocks, skip tool_use."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
        return "\n".join(p for p in parts if p).strip()
    return ""


def format_memory(session_id: str, cwd: str, reason: str, turns: list[dict]) -> str:
    """Format session turns as markdown for memory storage."""
    if not turns:
        return ""

    first_ts = turns[0].get("timestamp", "")
    last_ts = turns[-1].get("timestamp", "")
    user_count = sum(1 for t in turns if t["role"] == "user")
    asst_count = sum(1 for t in turns if t["role"] == "assistant")

    lines = []
    lines.append("# Claude Code Session")
    lines.append("")
    lines.append(f"**Session ID:** {session_id}")
    lines.append(f"**Working directory:** {cwd}")
    lines.append(f"**Ended:** {reason}")
    lines.append(f"**Range:** {first_ts} - {last_ts}")
    lines.append(f"**Turns:** {user_count} user, {asst_count} assistant")
    lines.append("")
    lines.append("## Conversation")
    lines.append("")

    for t in turns:
        role = "User" if t["role"] == "user" else "Assistant"
        lines.append(f"### {role}")
        lines.append("")
        lines.append(t["text"])
        lines.append("")

    return "\n".join(lines)


def post_create_memory(voitta_url: str, user_name: str, content: str) -> None:
    """POST create_memory to the voitta-rag MCP endpoint."""
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
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-User-Name": user_name,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            for line in body.splitlines():
                if line.startswith("data: "):
                    result = json.loads(line[6:])
                    break
            else:
                raise RuntimeError("No data frame in SSE response")
        else:
            result = json.loads(body)

        if "error" in result:
            raise RuntimeError(f"MCP error: {result['error']}")


def main() -> int:
    try:
        hook_input = read_hook_input()
    except Exception as e:
        print(f"voitta-rag session-memory: bad hook input: {e}", file=sys.stderr)
        return 0

    transcript_path_str = hook_input.get("transcript_path", "")
    session_id = hook_input.get("session_id", "unknown")
    cwd = hook_input.get("cwd", "")
    reason = hook_input.get("reason", "unknown")

    if not transcript_path_str:
        print("voitta-rag session-memory: no transcript_path in hook input", file=sys.stderr)
        return 0

    transcript_path = Path(transcript_path_str)
    if not transcript_path.exists():
        print(f"voitta-rag session-memory: transcript not found: {transcript_path}", file=sys.stderr)
        return 0

    try:
        turns = extract_turns(transcript_path)
    except Exception as e:
        print(f"voitta-rag session-memory: failed to read transcript: {e}", file=sys.stderr)
        return 0

    if not turns:
        print("voitta-rag session-memory: no user/assistant turns — skipping", file=sys.stderr)
        return 0

    content = format_memory(session_id, cwd, reason, turns)

    voitta_url = os.environ.get("VOITTA_URL", "http://localhost:8000")
    voitta_user = os.environ.get("VOITTA_USER", os.environ.get("USER", "anonymous"))

    try:
        post_create_memory(voitta_url, voitta_user, content)
        print(
            f"voitta-rag session-memory: saved session {session_id[:8]} "
            f"({len(turns)} turns) to {voitta_url}",
            file=sys.stderr,
        )
    except Exception as e:
        print(f"voitta-rag session-memory: failed to create memory: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
