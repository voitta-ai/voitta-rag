#!/usr/bin/env bash
#
# voitta-rag session memory hook (Stop event)
#
# On first invocation per Claude Code session, blocks the stop and asks
# the assistant to save a session summary as a voitta-rag memory.
# Subsequent invocations in the same session approve silently.
#
# Uses a marker file in /tmp keyed by parent PID (the Claude Code process).

MARKER="/tmp/voitta-session-memory-$$-${PPID}"

# Clean up stale markers older than 24 hours
find /tmp -maxdepth 1 -name "voitta-session-memory-*" -mmin +1440 -delete 2>/dev/null || true

if [[ -f "$MARKER" ]]; then
    # Already prompted this session — allow stop
    exit 0
fi

# First stop in this session — create marker and block
touch "$MARKER"

cat <<'EOF'
{"decision": "block", "reason": "Before ending this session, please save a brief summary of what was accomplished as a voitta-rag memory using the create_memory tool. Include: what was worked on, key decisions made, and any unfinished items. Then you may stop."}
EOF
