# voitta-rag Claude Code Plugin

Connects [Claude Code](https://docs.anthropic.com/en/docs/claude-code) to your voitta-rag instance.

## What it does

1. **MCP Server** — Configures voitta-rag as an MCP server in `~/.claude.json`, including the `X-User-Name` header so memory tools (`create_memory`, etc.) can identify the user.
2. **Session Memory Hook** (optional) — Installs a `SessionEnd` hook that reads the session transcript on exit and saves it as a voitta-rag memory via `create_memory`. One memory per session, created automatically when the session ends.

## Setup

```bash
# Basic: MCP server only
bash claude-plugin/setup.sh

# Docker mode (port 58000)
bash claude-plugin/setup.sh --docker

# With session memory hook
bash claude-plugin/setup.sh --docker --with-hook

# Custom user name (defaults to $USER)
bash claude-plugin/setup.sh --docker --with-hook --user alice

# Custom voitta-rag URL
bash claude-plugin/setup.sh --url http://my-server:8000
```

Restart Claude Code after setup.

## Uninstall

```bash
bash claude-plugin/setup.sh --uninstall
```

## How the session memory hook works

The hook uses Claude Code's `SessionEnd` event, which fires once per session when the user ends it (`/exit`, terminal close, etc.). Unlike `Stop` (which fires after every assistant turn), `SessionEnd` is exactly once per session.

When the hook runs:

1. Claude Code passes the hook a JSON payload on stdin containing `session_id`, `transcript_path`, `cwd`, and `reason`.
2. The hook reads the transcript JSONL file at `transcript_path`.
3. It extracts all user prompts and assistant text responses (tool calls are skipped).
4. It formats the conversation as markdown and POSTs it to voitta-rag's `create_memory` MCP tool.

The memory is then searchable via semantic search — useful for recalling past work ("what did I debug last week?", "when did I set up the Grafana alerts?").

## Configuration

The hook is configured via environment variables baked into the `settings.json` entry by `setup.sh`:

- `VOITTA_URL` — base URL of voitta-rag (e.g. `http://localhost:58000` for Docker)
- `VOITTA_USER` — user name for the `X-User-Name` header

Re-run `setup.sh --with-hook --user NAME` to change them.

## Failure handling

If the voitta-rag instance is down or the API call fails, the hook logs an error to stderr (visible in Claude Code's session-end output) but **never fails the session close**. We don't want a memory save error to block you from exiting.

## Importing past session history

To backfill existing Claude Code session history as memories:

```bash
python3 scripts/import_claude_history.py --voitta-url http://localhost:58000

# Filter by project
python3 scripts/import_claude_history.py --voitta-url http://localhost:58000 \
    --project /path/to/project

# Filter by date
python3 scripts/import_claude_history.py --voitta-url http://localhost:58000 \
    --after 2025-01-01

# Dry run first
python3 scripts/import_claude_history.py --dry-run
```

The import script parses `~/.claude/history.jsonl` (which stores user prompts only) and groups them by session. See `python3 scripts/import_claude_history.py --help` for all options.

Note: `history.jsonl` only has user prompts — not assistant responses. The `SessionEnd` hook, by contrast, reads the full transcript and captures both sides of the conversation.
