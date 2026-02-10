# voitta-rag

Web-based file management system with RAG indexing and real-time monitoring.

## Prerequisites

### Python

Requires Python 3.11+

If using pyenv, ensure these system libraries are installed first:

```bash
# Ubuntu/Debian
sudo apt-get install -y build-essential libssl-dev zlib1g-dev \
  libbz2-dev libreadline-dev libsqlite3-dev curl git \
  libncursesw5-dev xz-utils tk-dev libxml2-dev libxmlsec1-dev \
  libffi-dev liblzma-dev

# Then install Python
pyenv install 3.12
```

### Dependencies Note

The `transformers` library must be version 4.x (not 5.x) due to compatibility with `sentence-transformers`. This is already constrained in requirements.txt:

```
transformers>=4.36.0,<5.0.0
```

If you encounter `ModuleNotFoundError: Could not import module 'PreTrainedModel'`, downgrade transformers:

```bash
pip install "transformers>=4.36.0,<5.0.0"
```

## Quick Start

### Option A: Docker (recommended)

Starts both voitta-rag and Qdrant with persistent storage via Docker Compose:

```bash
cp .env.example .env
make docker-up
```

Open http://localhost:58000 in your browser. Stop with `make docker-down`.

By default, `~/.ssh` is mounted read-only into the container for SSH-based git access. Override with:

```bash
SSH_KEY_DIR=/path/to/ssh/keys docker compose up -d --build
```

### Option B: Local development

```bash
# Start Qdrant
mkdir -p qdrant_storage
docker run -d --name qdrant \
  -p 6333:6333 -p 6334:6334 \
  -v $(pwd)/qdrant_storage:/qdrant/storage \
  qdrant/qdrant

# Install and run
make install
cp .env.example .env
make run
```

Open http://localhost:8000 in your browser.

## Configuration

Key settings in `.env`:

```bash
# Root folder for managed files
ROOT_PATH=/mnt/ssddata/data/voitta-rag-data

# Qdrant connection
QDRANT_HOST=localhost
QDRANT_PORT=6333

# Embedding model (uses GPU if available)
EMBEDDING_MODEL=intfloat/e5-base-v2

# Chunking settings
CHUNK_SIZE=512
CHUNK_OVERLAP=50

# Indexing worker poll interval (seconds)
INDEXING_POLL_INTERVAL=10

# MCP server port
MCP_PORT=8001
```

## MCP Server (for Claude Code integration)

The MCP server runs embedded in the main app (no separate process needed) and exposes RAG capabilities via the [MCP protocol](https://modelcontextprotocol.io/).

### Claude Code Configuration

Add to `~/.claude.json` under `mcpServers` (global) or in your project settings:

```json
{
  "mcpServers": {
    "voitta-rag": {
      "type": "http",
      "url": "http://localhost:58000/mcp/mcp"
    }
  }
}
```

> **Note:** The URL path is `/mcp/mcp` — FastMCP creates its endpoint at `/mcp` inside the app, which is itself mounted at `/mcp`. If running locally (not Docker), replace `58000` with `8000`.

### Available MCP Tools

| Tool | Description |
|------|-------------|
| **`search`** | Hybrid semantic + keyword search across indexed documents |
| **`list_indexed_folders`** | List all indexed folders with status, file counts, and metadata |
| **`get_file`** | Get full content of an indexed file |
| **`get_chunk_range`** | Get a range of chunks from a file, merged with overlaps removed |
| **`get_file_uri`** | Get a download URI for a file (for use with wget/curl) |
| **`set_folder_active`** | Set folder visibility for search (requires `X-User-Name` header) |
| **`get_folder_active_states`** | Get active/inactive state of all folders for current user |

## Bulk Repository Import

Import multiple Git repositories at once using a JSON config file:

```bash
python3 scripts/import_repos.py [path/to/config.json]
```

Defaults to `scripts/import_repos.json`. Copy the example to get started:

```bash
cp scripts/import_repos.example.json scripts/import_repos.json
```

The config specifies per-host auth and folders with repo lists:

```json
{
    "hosts": {
        "github.com": {"auth_method": "ssh"},
        "git.example.com": {
            "auth_method": "token",
            "username": "your-username",
            "token": "your-pat-token"
        }
    },
    "folders": {
        "my-repos": [
            {"repo": "git@github.com:org/repo.git"},
            {"repo": "https://git.example.com/team/project.git", "branch": "develop"}
        ]
    }
}
```

Branch is auto-detected from the remote when not specified. The `import_repos.json` file is gitignored (may contain credentials).

## Features

- File browser with real-time updates
- Folder creation and file upload
- Per-user folder enable/disable for indexing
- Automatic document indexing (DOCX, PPTX, XLSX, ODT, ODP, ODS)
- Vector search with Qdrant
- MCP server for Claude Code integration
- File change detection via content hashing
- Global file/folder metadata
- Dark/light theme support
