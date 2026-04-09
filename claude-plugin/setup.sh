#!/usr/bin/env bash
#
# voitta-rag Claude Code plugin setup
#
# Configures:
#   1. MCP server in ~/.claude.json with X-User-Name header
#   2. (Optional) SessionEnd hook that saves a session transcript as a memory
#
# Usage:
#   bash claude-plugin/setup.sh [--with-hook] [--url URL] [--user NAME]
#
# Options:
#   --with-hook   Install SessionEnd hook that saves session as a memory
#   --url URL     voitta-rag base URL (default: http://localhost:8000)
#   --docker      Use Docker URL (http://localhost:58000)
#   --user NAME   User name for X-User-Name header (default: $USER)
#   --uninstall   Remove voitta-rag MCP server and hook

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_JSON="$HOME/.claude.json"
SETTINGS_JSON="$HOME/.claude/settings.json"
HOOK_SCRIPT="$SCRIPT_DIR/hooks/session-memory.sh"

VOITTA_URL="http://localhost:8000"
VOITTA_USER="${USER:-anonymous}"
INSTALL_HOOK=false
UNINSTALL=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-hook) INSTALL_HOOK=true; shift ;;
        --url) VOITTA_URL="$2"; shift 2 ;;
        --docker) VOITTA_URL="http://localhost:58000"; shift ;;
        --user) VOITTA_USER="$2"; shift 2 ;;
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
        # Remove voitta-rag hook from both Stop (legacy) and SessionEnd
        jq --arg cmd "$HOOK_SCRIPT" '
            if .hooks.Stop then
                .hooks.Stop = [.hooks.Stop[] | select((.hooks // [])[0].command != $cmd)]
                | if .hooks.Stop == [] then del(.hooks.Stop) else . end
            else . end
            | if .hooks.SessionEnd then
                .hooks.SessionEnd = [.hooks.SessionEnd[] | select((.hooks // [])[0].command != $cmd)]
                | if .hooks.SessionEnd == [] then del(.hooks.SessionEnd) else . end
            else . end
        ' "$SETTINGS_JSON" > "${SETTINGS_JSON}.tmp" \
            && mv "${SETTINGS_JSON}.tmp" "$SETTINGS_JSON"
        echo "  Removed hooks from $SETTINGS_JSON"
    fi

    echo "Done."
    exit 0
fi

# --- Install MCP server ---
echo "Configuring voitta-rag MCP server..."

if [[ ! -f "$CLAUDE_JSON" ]]; then
    echo '{}' > "$CLAUDE_JSON"
fi

jq --arg url "$MCP_URL" --arg user "$VOITTA_USER" '
    .mcpServers["voitta-rag"] = {
        "type": "http",
        "url": $url,
        "headers": {"X-User-Name": $user}
    }
' "$CLAUDE_JSON" > "${CLAUDE_JSON}.tmp" \
    && mv "${CLAUDE_JSON}.tmp" "$CLAUDE_JSON"

echo "  MCP server configured at $MCP_URL"
echo "  X-User-Name: $VOITTA_USER"
echo "  Config: $CLAUDE_JSON"

# --- Install SessionEnd hook ---
if $INSTALL_HOOK; then
    echo ""
    echo "Installing session memory hook..."

    chmod +x "$HOOK_SCRIPT"

    if [[ ! -f "$SETTINGS_JSON" ]]; then
        mkdir -p "$(dirname "$SETTINGS_JSON")"
        echo '{}' > "$SETTINGS_JSON"
    fi

    # Clean any legacy Stop hook pointing at the same script
    jq --arg cmd "$HOOK_SCRIPT" '
        if .hooks.Stop then
            .hooks.Stop = [.hooks.Stop[] | select((.hooks // [])[0].command != $cmd)]
            | if .hooks.Stop == [] then del(.hooks.Stop) else . end
        else . end
    ' "$SETTINGS_JSON" > "${SETTINGS_JSON}.tmp" \
        && mv "${SETTINGS_JSON}.tmp" "$SETTINGS_JSON"

    # Check if SessionEnd hook already exists for this script
    HOOK_EXISTS=$(jq --arg cmd "$HOOK_SCRIPT" '
        [(.hooks.SessionEnd // [])[] | .hooks[]? | .command] | any(. == $cmd)
    ' "$SETTINGS_JSON")

    HOOK_ENTRY=$(jq -n \
        --arg cmd "$HOOK_SCRIPT" \
        --arg url "$VOITTA_URL" \
        --arg user "$VOITTA_USER" \
        '{
            "hooks": [{
                "type": "command",
                "command": "VOITTA_URL=\($url) VOITTA_USER=\($user) \($cmd)"
            }]
        }')

    if [[ "$HOOK_EXISTS" == "true" ]]; then
        # Replace existing entry to pick up latest URL/user
        jq --arg cmd "$HOOK_SCRIPT" --argjson entry "$HOOK_ENTRY" '
            .hooks.SessionEnd = [
                (.hooks.SessionEnd[] | select((.hooks // [])[0].command | contains($cmd) | not))
            ] + [$entry]
        ' "$SETTINGS_JSON" > "${SETTINGS_JSON}.tmp" \
            && mv "${SETTINGS_JSON}.tmp" "$SETTINGS_JSON"
        echo "  SessionEnd hook updated"
    else
        jq --argjson entry "$HOOK_ENTRY" '
            .hooks.SessionEnd = (.hooks.SessionEnd // []) + [$entry]
        ' "$SETTINGS_JSON" > "${SETTINGS_JSON}.tmp" \
            && mv "${SETTINGS_JSON}.tmp" "$SETTINGS_JSON"
        echo "  SessionEnd hook installed"
    fi

    echo "  VOITTA_URL=$VOITTA_URL VOITTA_USER=$VOITTA_USER"
    echo "  Config: $SETTINGS_JSON"
fi

echo ""
echo "Setup complete. Restart Claude Code to apply changes."
