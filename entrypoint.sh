#!/bin/sh
# Ensure optional files exist before starting the app
touch /data/users.txt 2>/dev/null || true

# Generate MinerU config pointing to the mounted models directory
MODELS_DIR="${MINERU_MODELS_DIR:-/root/.cache/mineru-models}"
mkdir -p "$MODELS_DIR"
cat > /root/magic-pdf.json <<EOFCFG
{
  "models-dir": "$MODELS_DIR",
  "layout-config": {
    "model": "doclayout_yolo"
  },
  "formula-config": {
    "enable": false
  },
  "table-config": {
    "enable": false
  },
  "ocr-config": {
    "enable": false
  }
}
EOFCFG

exec "$@"
