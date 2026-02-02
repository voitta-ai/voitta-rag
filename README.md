# voitta-rag

Web-based file management system with RAG indexing and real-time monitoring.

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

# Run the server
uvicorn src.voitta.main:app --reload
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
```

## Features

- File browser with real-time updates
- Folder creation and file upload
- Per-user folder enable/disable for indexing
- Automatic document indexing (DOCX, PPTX, XLSX, ODT, ODP, ODS)
- Vector search with Qdrant
- File change detection via content hashing
- Global file/folder metadata
- Dark/light theme support
