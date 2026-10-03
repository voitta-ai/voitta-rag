#!/usr/bin/env python3
"""SessionEnd hook that creates a voitta-rag memory from a Claude Code session.

Reads hook input JSON from stdin, then reads the transcript JSONL file pointed
to by transcript_path. Extracts user prompts and assistant text responses,
formats them as markdown, redacts credential-shaped substrings, and POSTs to
voitta-rag's create_memory MCP tool.

Configured via env vars (set by setup.sh):
    VOITTA_URL   voitta-rag base URL (default: http://localhost:8000)
    VOITTA_USER  X-User-Name header  (default: $USER)

Failures are logged to stderr but do not fail the hook — we never want to
break the user's session close on a memory save error.
"""

import json
import os
import re
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


REDACTED = "[REDACTED-SECRET]"

# Credential shapes to strip before a transcript is persisted and embedded.
# A memory store is long-lived and retrievable, so anything that lands here can
# resurface in a later session's context long after the original secret was
# rotated -- or, worse, before.
#
# On top of that, every detector reports the character spans it would redact;
# the union of all spans is then replaced. Detectors never see each
# other's output, so their order cannot matter (one cannot split a token another
# would have taken whole), and widening coverage can only add spans: anything an
# earlier detector redacted stays redacted.
#
# The redactor exactly as shipped in #53, kept verbatim and applied FIRST, so
# the layers below can only add redaction: nothing #53 removed can reappear.
_LEGACY_PATTERNS = [
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"glpat-[A-Za-z0-9\-_]{20,}"),
    re.compile(r"xox[baprse]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"xapp-[0-9]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(r"(?<=://)([^/\s:@]+):([^/\s@]+)(?=@)"),
]
_LEGACY_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|secret|password|passwd|token)\b(\s*[=:]\s*[\"']?)"
    r"([A-Za-z0-9/+=_\-]{16,})",
)


def _legacy_redact(text):
    for pattern in _LEGACY_PATTERNS[:-1]:
        text = pattern.sub(REDACTED, text)
    text = _LEGACY_PATTERNS[-1].sub(r"\1:" + REDACTED, text)
    text = _LEGACY_ASSIGNMENT.sub(r"\1\2" + REDACTED, text)
    retval = text
    return retval


# (pattern, group): the group whose span is the secret; 0 is the whole match.
_SPAN_PATTERNS = [
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), 0),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), 0),
    (re.compile(r"glpat-[A-Za-z0-9\-_]{20,}"), 0),
    # Slack: xox?- are bot/user/app tokens, xapp- is an app-level token,
    # which is a different shape and is easy to miss.
    (re.compile(r"xox[baprse]-[A-Za-z0-9\-]{10,}"), 0),
    (re.compile(r"xapp-[0-9]-[A-Za-z0-9\-]{10,}"), 0),
    (re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"), 0),
    # OpenAI: legacy sk- keys, and project/service-account/admin keys whose
    # sk-proj- style prefix carries a hyphen the legacy form does not allow.
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), 0),
    (re.compile(r"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{20,}"), 0),
    # JWTs: compact JWS whose header is base64url JSON. '{' followed by '"',
    # a space, a tab or a newline encodes as "ey" or "ew". The payload is not
    # assumed ({} is e30). The lookbehind keeps failed matches from rescanning.
    (re.compile(r"(?<![A-Za-z0-9_\-])e[wy][A-Za-z0-9_\-]{14,}\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]{10,}"), 0),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), 0),
    # URLs of the form scheme://user:secret@host, which is how a PAT ends up in
    # a git remote and therefore in any transcript that echoed one.
    (re.compile(r"(?<=://)([^/\s:@]+):([^/\s@]+)(?=@)"), 2),
    # Authorization: Bearer <token>. Scoped to the header; whatever follows the
    # scheme is a credential, however short. Prose shaped exactly like the
    # header is over-redacted, the cheaper error.
    (re.compile(r"(?i)\bauthorization[\"']?\s*[:=]\s*[\"']?bearer\s+([^\s\"']+)"), 1),
    # Assignment-shaped secrets, as shipped in #53: value only, name kept.
    (re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token)\b(\s*[=:]\s*[\"']?)([A-Za-z0-9/+=_\-]{16,})"), 3),
    # Assignments with a quoted value: everything to the matching unescaped
    # quote, however short (passphrases with spaces, escaped quotes). The name
    # may carry a prefix (API_TOKEN, DB_PASSWORD) and the key may be quoted.
    (re.compile(
        r"(?i)\b(?:[a-z0-9]+_)*(?:api[_-]?key|secret|password|passwd|token)\b"
        r"[\"']?\s*[=:]\s*([\"'])((?:\\.|(?!\1)[^\\\n])+)(?=\1)"
    ), 2),
]

# A credential glued to the next key (ghp_...token=value) is consumed whole,
# label included, so the value is left with only a marker in front of it. A
# marker directly followed by an assignment operator therefore means "a key was
# eaten here": redact the value too.
_SPAN_PATTERNS.append((
    re.compile(re.escape(REDACTED) + r"[\"']?\s*[=:]\s*[\"']?([^\s\"',;<>]{1,})"),
    1,
))
# ...and the same with a quoted value, taken to the matching unescaped quote.
_SPAN_PATTERNS.append((
    re.compile(re.escape(REDACTED) + r"[\"']?\s*[=:]\s*([\"'])((?:\\.|(?!\1)[^\\\n])+)(?=\1)"),
    2,
))

# Assignments with an unquoted value (or a quote that never closes, e.g. a
# truncated line). The value runs to whitespace or a delimiter, so password
# punctuation (p@ss!word) is kept whole. Filtered in _unquoted_spans, not in
# the regex, to stay linear.
_UNQUOTED_ASSIGNMENT = re.compile(
    r"(?i)\b(?:[a-z0-9]+_)*(?:api[_-]?key|secret|password|passwd|token)\b"
    r"[\"']?\s*[=:]\s*[\"']?([^\s\"',;<>]{8,})"
)
# Code, not a credential: a call on an identifier chain (lexer.next_token()).
# Plain attribute access (response.access_token) is deliberately NOT exempt:
# letter-only passwords exist, and a false positive is the cheaper error.
_CALL_EXPRESSION = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\(\)?")


def _unquoted_spans(text):
    spans = []
    for match in _UNQUOTED_ASSIGNMENT.finditer(text):
        if _CALL_EXPRESSION.fullmatch(match.group(1)):
            continue
        spans.append(match.span(1))
    retval = spans
    return retval


def redact_secrets(text: str) -> str:
    """Replace credential-shaped substrings with a marker.

    Deliberately conservative about what it keeps: a false positive costs a
    little readability in a stored memory, a false negative persists a live
    credential in an embedded, searchable store.
    """
    # Matches of one pattern cannot overlap, so a value that runs straight into
    # the next key ("...cdeaeX-Api-Key: ...") hides that second assignment from
    # the first pass. A pass only ever inserts markers, which break such runs,
    # so repeat until nothing changes; each pass is linear.
    retval = _legacy_redact(text)
    for _ in range(4):
        redacted = _redact_pass(retval)
        if redacted == retval:
            break
        retval = redacted
    return retval


def _redact_pass(text):
    spans = []
    for pattern, group in _SPAN_PATTERNS:
        for match in pattern.finditer(text):
            spans.append(match.span(group))
    spans.extend(_unquoted_spans(text))

    merged = []
    for begin, end in sorted(s for s in spans if s[1] > s[0]):
        if merged and begin <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([begin, end])

    pieces = []
    cursor = 0
    for begin, end in merged:
        pieces.append(text[cursor:begin])
        pieces.append(REDACTED)
        cursor = end
    pieces.append(text[cursor:])
    retval = "".join(pieces)
    return retval


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

    content = redact_secrets(format_memory(session_id, cwd, reason, turns))

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
