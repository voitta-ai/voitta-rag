# voitta-rag

Web-based file management system with real-time monitoring.

## Quick Start

```bash
# Install dependencies
pip install -e ".[dev]"

# Run the server
uvicorn src.voitta.main:app --reload
```

Open http://localhost:8000 in your browser.

## Features

- File browser with real-time updates
- Folder creation and file upload
- Per-user folder enable/disable for downstream indexing
- Global file/folder metadata
- Dark/light theme support
