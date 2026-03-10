#!/bin/sh
# Ensure optional files exist before starting the app
touch /data/users.txt 2>/dev/null || true
exec "$@"
