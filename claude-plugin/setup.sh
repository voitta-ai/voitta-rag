#!/usr/bin/env bash
#
# voitta-rag Claude Code plugin setup
#
# Configures:
#   1. MCP server in ~/.claude.json
#   2. (Optional) Stop hook that prompts session memory creation
#
# Usage:
#   bash claude-plugin/setup.sh [--with-hook] [--url URL]
#
# Options:
#   --with-hook   Install the Stop hook for session memory creation
#   --url URL     voitta-rag base URL (default: http://localhost:8000)
#   --docker      Use Docker URL (http://localhost:58000)
#   --uninstall   Remove voitta-rag MCP server and hook

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_JSON="$HOME/.claude.json"
SETTINGS_JSON="$HOME/.claude/settings.json"
HOOK_SCRIPT="$SCRIPT_DIR/hooks/session-memory.sh"

VOITTA_URL="http://localhost:8000"
INSTALL_HOOK=false
UNINSTALL=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-hook) INSTALL_HOOK=true; shift ;;
        --url) VOITTA_URL="$2"; shift 2 ;;
        --docker) VOITTA_URL="http://localhost:58000"; shift ;;
        --uninstall) UNINSTALL=true; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

MCP_URL="${VOITTA_URL}/mcp/mcp"

# Ensure jq is available
if ! command -v jq &>/dev/null; then
    echo "Error: jq is required. Install with: brew install jq (macOS) or apt install jq (Linux)"
    exit 1
fi

# --- Uninstall ---
if $UNINSTALL; then
    echo "Removing voitta-rag from Claude Code configuration..."

    if [[ -f "$CLAUDE_JSON" ]]; then
        jq 'del(.mcpServers["voitta-rag"])' "$CLAUDE_JSON" > "${CLAUDE_JSON}.tmp" \
            && mv "${CLAUDE_JSON}.tmp" "$CLAUDE_JSON"
        echo "  Removed MCP server from $CLAUDE_JSON"
    fi

    if [[ -f "$SETTINGS_JSON" ]]; then
        jq 'if .hooks.Stop then .hooks.Stop = [.hooks.Stop[] | select(.hooks[0].command != "'"$HOOK_SCRIPT"'")] | if .hooks.Stop == [] then del(.hooks.Stop) else . end else . end' \
            "$SETTINGS_JSON" > "${SETTINGS_JSON}.tmp" \
            && mv "${SETTINGS_JSON}.tmp" "$SETTINGS_JSON"
        echo "  Removed Stop hook from $SETTINGS_JSON"
    fi

    echo "Done."
    exit 0
fi

# --- Install MCP server ---
echo "Configuring voitta-rag MCP server..."

if [[ ! -f "$CLAUDE_JSON" ]]; then
    echo '{}' > "$CLAUDE_JSON"
fi

jq --arg url "$MCP_URL" '.mcpServers["voitta-rag"] = {"type": "http", "url": $url}' \
    "$CLAUDE_JSON" > "${CLAUDE_JSON}.tmp" \
    && mv "${CLAUDE_JSON}.tmp" "$CLAUDE_JSON"

echo "  MCP server configured at $MCP_URL"
echo "  Config: $CLAUDE_JSON"

# --- Install Stop hook ---
if $INSTALL_HOOK; then
    echo ""
    echo "Installing session memory hook..."

    chmod +x "$HOOK_SCRIPT"

    if [[ ! -f "$SETTINGS_JSON" ]]; then
        mkdir -p "$(dirname "$SETTINGS_JSON")"
        echo '{}' > "$SETTINGS_JSON"
    fi

    # Check if hook already exists
    HOOK_EXISTS=$(jq --arg cmd "$HOOK_SCRIPT" '
        .hooks.Stop // [] | any(.hooks[]; .command == $cmd)
    ' "$SETTINGS_JSON")

    if [[ "$HOOK_EXISTS" == "true" ]]; then
        echo "  Hook already installed"
    else
        jq --arg cmd "$HOOK_SCRIPT" '
            .hooks.Stop = (.hooks.Stop // []) + [{"hooks": [{"type": "command", "command": $cmd}]}]
        ' "$SETTINGS_JSON" > "${SETTINGS_JSON}.tmp" \
            && mv "${SETTINGS_JSON}.tmp" "$SETTINGS_JSON"
        echo "  Stop hook installed: $HOOK_SCRIPT"
    fi

    echo "  Config: $SETTINGS_JSON"
fi

echo ""
echo "Setup complete. Restart Claude Code to apply changes."
