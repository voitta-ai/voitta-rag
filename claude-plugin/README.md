# voitta-rag Claude Code Plugin

Connects [Claude Code](https://docs.anthropic.com/en/docs/claude-code) to your voitta-rag instance.

## What it does

1. **MCP Server** — Configures voitta-rag as an MCP server so Claude Code can search your indexed documents and memories.
2. **Session Memory Hook** (optional) — Installs a `Stop` hook that prompts Claude to save a summary of each session as a voitta-rag memory before ending.

## Setup

```bash
# Basic: MCP server only
bash claude-plugin/setup.sh

# With session memory hook
bash claude-plugin/setup.sh --with-hook

# Docker mode (port 58000)
bash claude-plugin/setup.sh --docker

# Custom URL
bash claude-plugin/setup.sh --url http://my-server:8000

# Combine options
bash claude-plugin/setup.sh --docker --with-hook
```

Restart Claude Code after setup.

## Uninstall

```bash
bash claude-plugin/setup.sh --uninstall
```

## How the session memory hook works

When installed, the hook runs each time Claude finishes responding. On the **first stop** of a session, it asks Claude to save a brief summary of what was accomplished as a memory. Subsequent stops in the same session are unaffected.

Memories are stored in your voitta-rag Anamnesis folder and become searchable via semantic search — useful for recalling past work ("what did I debug last week?").

## Importing past session history

To import your existing Claude Code session history as memories:

```bash
python3 scripts/import_claude_history.py

# Filter by project
python3 scripts/import_claude_history.py --project /path/to/project

# Filter by date
python3 scripts/import_claude_history.py --after 2025-01-01

# Dry run first
python3 scripts/import_claude_history.py --dry-run
```

See `python3 scripts/import_claude_history.py --help` for all options.
