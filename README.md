# voitta-rag

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

A self-hosted knowledge base that indexes your documents and code repositories, making them searchable through semantic (RAG) search. Connect it to AI coding assistants like Claude Code via MCP, or use the built-in web UI to browse and search across all your indexed content.

Useful for teams and individuals who want to:
- **Search across codebases and docs** — index Git repos, Google Drive folders, Jira boards, and office documents in one place
- **Give AI assistants context** — expose your indexed knowledge to Claude Code or other MCP-compatible tools
- **Keep everything local** — runs on your infrastructure with Qdrant for vector storage, no data leaves your network

## Features

- File browser with real-time updates
- Folder creation and file upload
- Git repository sync and indexing
- Google Drive integration
- Jira board sync
- Per-user folder enable/disable for indexing
- Automatic document indexing (DOCX, PPTX, XLSX, ODT, ODP, ODS)
- Vector search with Qdrant
- MCP server for Claude Code integration
- File change detection via content hashing
- Global file/folder metadata
- Dark/light theme support

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

#### Mounting local directories

To index a local directory (e.g., Google Drive for Desktop) without using a remote sync connector, create a `docker-compose.override.yml` (gitignored, merged automatically by Docker Compose):

```yaml
services:
  voitta-rag:
    volumes:
      - ~/Google Drive:/data/fs/gdrive:ro
```

The directory appears as a folder named `gdrive` in the UI. Enable indexing on it like any other folder. The file watcher detects changes automatically.

You can mount multiple directories:

```yaml
services:
  voitta-rag:
    volumes:
      - ~/Google Drive:/data/fs/gdrive:ro
      - ~/Dropbox/Projects:/data/fs/dropbox-projects:ro
```

Note: symlinks inside mounted volumes won't work -- Docker doesn't resolve symlink targets across mount boundaries. Use volume mounts instead.

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

