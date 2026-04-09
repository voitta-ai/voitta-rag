#!/usr/bin/env bash
#
# voitta-rag SessionEnd hook
#
# On Claude Code session end, reads the session transcript and creates a
# voitta-rag memory summarising the user prompts from the session.
#
# Config via env vars (set by setup.sh):
#   VOITTA_URL   voitta-rag base URL (default: http://localhost:8000)
#   VOITTA_USER  X-User-Name header (default: $USER)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/session-memory.py"
