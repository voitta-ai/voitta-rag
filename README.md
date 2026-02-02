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

### 1. Start Qdrant (Vector Database)

```bash
# Create persistent storage directory
mkdir -p qdrant_storage

# Run Qdrant with Docker
docker run -d \
  --name qdrant \
  -p 6333:6333 \
  -p 6334:6334 \
  -v $(pwd)/qdrant_storage:/qdrant/storage \
  qdrant/qdrant
```

### 2. Install and Run

```bash
# Install dependencies
pip install -e ".[dev]"

# Copy environment template and configure
cp .env.example .env

# Run the server (binds to 0.0.0.0:8000 by default)
uvicorn src.voitta.main:app --reload --host 0.0.0.0
```

Open http://localhost:8000 in your browser.

## Configuration

Key settings in `.env`:

```bash
# Root folder for managed files
ROOT_PATH=./data

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

The MCP server exposes RAG capabilities for Claude Code and other MCP clients.

### Start the MCP Server

```bash
# Run alongside the main app (in a separate terminal)
python -m src.voitta.mcp_server
```

The MCP server runs on port 8001 by default. Configure via `.env`:
- `MCP_PORT` - Server port (default: 8001)
- `MCP_TRANSPORT` - `streamable-http` (default) or `sse` (required for Claude Code)

### Available MCP Tools

**`search`** - Semantic search across indexed documents
- `query`: Search text
- `limit`: Max results (default: 10)
- `include_folders`: Optional list of folders to search within
- `exclude_folders`: Optional list of folders to exclude

**`list_indexed_folders`** - List all indexed folders with status and metadata

**`get_file`** - Retrieve full content of an indexed file by path

### Claude Code Configuration

1. Set `MCP_TRANSPORT=sse` in your `.env` file
2. Add to your Claude Code MCP settings:

```json
{
  "mcpServers": {
    "voitta-rag": {
      "url": "http://localhost:8001/sse"
    }
  }
}
```

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
