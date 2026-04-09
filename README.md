# voitta-rag

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

A self-hosted knowledge base that indexes your documents and code repositories, making them searchable through semantic (RAG) search. Connect it to AI coding assistants like Claude Code via MCP, or use the built-in web UI to browse and search across all your indexed content.

Useful for teams and individuals who want to:
- **Search across codebases and docs** — index Git repos, Google Drive folders, Jira boards, and office documents in one place
- **Give AI assistants context** — expose your indexed knowledge to Claude Code or other MCP-compatible tools
- **Keep everything local** — runs on your infrastructure with Qdrant for vector storage, no data leaves your network

## Table of Contents

- [Features](#features)
- [Search Scope](#search-scope)
- [Prerequisites](#prerequisites)
  - [Python](#python)
  - [Dependencies Note](#dependencies-note)
- [Quick Start](#quick-start)
  - [Option A: Docker (recommended)](#option-a-docker-recommended)
  - [Option B: Local development](#option-b-local-development)
- [Configuration](#configuration)
  - [Authentication](#authentication)
- [MCP Server (for Claude Code integration)](#mcp-server-for-claude-code-integration)
  - [Claude Code Plugin (automated setup)](#claude-code-plugin-automated-setup)
  - [Manual Claude Code Configuration](#manual-claude-code-configuration)
  - [Available MCP Tools](#available-mcp-tools)
  - [Importing Claude Code Session History](#importing-claude-code-session-history)
- [Concepts](#concepts)
  - [Projects](#projects)
  - [Toggle Switches](#toggle-switches)
  - [Anamnesis](#anamnesis)
- [Bulk Repository Import](#bulk-repository-import)

## Features

- File browser with real-time updates
- Folder creation and file upload
- Data source connectors: Filesystem (local path mapping), Git, Google Drive, SharePoint, Azure DevOps, Jira, Confluence, Box
- Jira/Confluence support for both Cloud and Server/Data Center deployments
- Per-user search scope control per folder
- Automatic document indexing (DOCX, PPTX, XLSX, ODT, ODP, ODS, GDOC, GSHEET, GSLIDES)
- Vector search with Qdrant (hybrid semantic + keyword, with time range filtering)
- MCP server for Claude Code integration
- Source URL resolution: resolve Google Docs/Sheets/Slides URLs to indexed content via MCP
- Anamnesis: persistent RAG memory for AI assistants (create, retrieve, like/dislike memories)
- File change detection via content hashing
- Global file/folder metadata
- Dark/light theme support

## Search Scope

Each folder in the file browser has a **Search** toggle that controls whether its content is included in search results.

<img width="1089" height="207" alt="image" src="https://github.com/user-attachments/assets/886f6914-1777-4330-94a8-34b2c349b913" />


When the toggle is **on** (green), the folder's documents are returned by search queries. When **off** (grey), the folder remains indexed but is invisible to search.

This is useful when you work on multiple unrelated projects. For example, suppose you have two folders indexed: `a-project` (your current client engagement) and `b-project` (an internal tool). While working on `a-project`, you don't want search results polluted with code and docs from `b-project` -- unrelated hits add noise and can mislead an AI assistant that consumes the results. Toggle `b-project` off, and searches only return content from `a-project`. When you switch contexts, flip the toggles.

The setting is **per-user and per-project** -- each user can have their own search scope without affecting others.

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

Runs both voitta-rag and Qdrant in containers. No Python installation needed on the host. The web UI is at port **58000**, the MCP endpoint at `/mcp/mcp` on the same port.

```bash
cp .env.example .env
make docker-up
```

Open http://localhost:58000 in your browser. Stop with `make docker-down`.

By default, `~/.ssh` is mounted read-only into the container for SSH-based git access. Override with:

```bash
SSH_KEY_DIR=/path/to/ssh/keys docker compose up -d --build
```

#### Mounting local directories (Mapped Paths)

In Docker mode, local directories are mounted into the container via `docker-compose.override.yml` (gitignored, merged automatically by Docker Compose). Each mounted directory appears automatically in the UI as a folder with a "Mapped Path" badge. The directory name becomes the folder name, so use descriptive names:

```yaml
services:
  voitta-rag:
    volumes:
      - ~/Google Drive:/data/fs/Google Drive:ro
```

The folder appears as "Google Drive" in the UI. Enable indexing on it like any other folder. The file watcher detects changes automatically.

Multiple directories:

```yaml
services:
  voitta-rag:
    volumes:
      - ~/Google Drive:/data/fs/Google Drive:ro
      - ~/Dropbox/Projects:/data/fs/Dropbox Projects:ro
```

Mapped Path folders cannot be created or deleted from the UI -- they are managed entirely through volume mounts. Upload is also disabled for these folders since the source of truth is the host directory. Restart with `make docker-up` after changing mounts.

Note: symlinks inside mounted volumes won't work -- Docker doesn't resolve symlink targets across mount boundaries. Use volume mounts instead.

### Option B: Local development

Runs voitta-rag directly with Python on your machine. Requires Python 3.11+ (see [Prerequisites](#prerequisites)). Qdrant still runs in Docker. The web UI is at port **8000**, the MCP endpoint at `/mcp/mcp` on the same port.

```bash
# Start Qdrant (vector database)
mkdir -p qdrant_storage
docker run -d --name qdrant \
  -p 6333:6333 -p 6334:6334 \
  -v $(pwd)/qdrant_storage:/qdrant/storage \
  qdrant/qdrant

# Install Python dependencies and run
make install
cp .env.example .env
make run
```

Open http://localhost:8000 in your browser.

**Key difference from Docker mode:** In local mode, voitta-rag has direct filesystem access — no volume mounts needed. Set `VOITTA_ROOT_PATH` in `.env` to the directory where indexed data should be stored.

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

# Microsoft login (Azure AD / Entra ID) — optional
MS_AUTH_TENANT_ID=
MS_AUTH_CLIENT_ID=
MS_AUTH_CLIENT_SECRET=

# Google login (OAuth2) — optional
GOOGLE_AUTH_CLIENT_ID=
GOOGLE_AUTH_CLIENT_SECRET=

# Base URL for OAuth redirect callbacks
VOITTA_BASE_URL=https://your-domain.com
```

### Authentication

The web UI supports optional OAuth login via **Microsoft (Azure AD)** and/or **Google**. Set the corresponding env vars to enable each provider. When any provider is configured, the landing page shows login buttons instead of the user picker.

For Microsoft, you need an Azure AD app registration with redirect URI `{VOITTA_BASE_URL}/auth/microsoft/callback`. For Google, create OAuth credentials in Google Cloud Console with redirect URI `{VOITTA_BASE_URL}/auth/google/callback`.

The MCP server validates tokens independently via `X-Auth-Token-Microsoft` and `X-Auth-Token-Google` headers — calling Microsoft Graph `/me` and Google userinfo respectively. Every tool response includes an `_auth` block with per-provider validation status.

## MCP Server (for Claude Code integration)

The MCP server runs embedded in the main app (no separate process needed) and exposes RAG capabilities via the [MCP protocol](https://modelcontextprotocol.io/).

### Claude Code Plugin (automated setup)

The fastest way to connect Claude Code to voitta-rag:

```bash
# Docker mode (port 58000)
bash claude-plugin/setup.sh --docker

# Local mode (port 8000)
bash claude-plugin/setup.sh

# With session memory hook (saves a session summary on exit)
bash claude-plugin/setup.sh --docker --with-hook
```

The plugin configures the MCP server in `~/.claude.json` and optionally installs a `Stop` hook that prompts Claude to save a session summary as a memory. See [claude-plugin/README.md](claude-plugin/README.md) for details.

### Manual Claude Code Configuration

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
| **`search`** | Hybrid semantic + keyword search across indexed documents. Supports `date_start`/`date_end` for time range filtering. |
| **`list_indexed_folders`** | List all indexed folders with status, file counts, and metadata |
| **`get_file`** | Get full content of an indexed file |
| **`get_chunk_range`** | Get a range of chunks from a file, merged with overlaps removed |
| **`get_file_uri`** | Get a download URI for a file (for use with wget/curl) |
| **`resolve_url`** | Resolve an external URL (Google Docs, Sheets, Slides) to indexed content |
| **`set_folder_active`** | Set folder visibility for search (requires `X-User-Name` header) |
| **`get_folder_active_states`** | Get active/inactive state of all folders for current user |
| **`create_memory`** | Create a persistent memory entry (Anamnesis) |
| **`get_memory`** | Retrieve a specific memory by ID |
| **`update_memory`** | Update content of an existing memory |
| **`delete_memory`** | Delete a memory entry |
| **`like_memory`** | Upvote a memory (increases relevance) |
| **`dislike_memory`** | Downvote a memory (decreases relevance) |
| **`list_memories`** | List all stored memories |

### Importing Claude Code Session History

Import your past Claude Code sessions as searchable memories:

```bash
python3 scripts/import_claude_history.py
```

This parses `~/.claude/history.jsonl`, groups prompts by session, and creates one memory per session. Filter what gets imported:

```bash
# Only sessions in a specific project
python3 scripts/import_claude_history.py --project /path/to/project

# Only sessions after a date
python3 scripts/import_claude_history.py --after 2025-06-01

# Only sessions mentioning a keyword
python3 scripts/import_claude_history.py --keyword "DynamoDB"

# Preview without importing
python3 scripts/import_claude_history.py --dry-run
```

Note: Only user prompts are stored locally by Claude Code; assistant responses are not available. Despite this, user prompts provide good semantic search targets for recalling past work.

## Concepts

### Projects

The project dropdown in the toolbar lets you organize which indexed folders are included in MCP search results. Each user gets a **Default** project automatically. You can create additional projects to group folders for different contexts — e.g., "Backend", "Client Docs", "Research".

- **Switching projects** changes which folders' search toggles are active. A folder can be search-active in one project but not in another.
- **Default project** stores search-active states globally in the user's folder settings. Non-default projects store their own independent set of search-active states.
- The active project persists across sessions and is used by the MCP server to determine which folders to search.
- Selecting "Manage Projects..." opens a modal to create or delete projects. The Default project cannot be deleted.

### Toggle Switches

Two independent toggles control folder behavior:

**Enable for Indexing** (sidebar toggle) — controls whether the background indexing worker processes a folder. When enabled, the folder is queued with "Pending" status and the worker will index its files into the vector store. Disabling the toggle stops future indexing but does not remove already-indexed content.

**Search** (inline checkbox in the file list) — controls whether a folder's indexed content is included in MCP search results. This is project-scoped: toggling it applies only to the currently active project. Toggling a parent folder applies recursively to all subfolders. A folder must be both indexed and search-active to appear in search results.

### Anamnesis

Anamnesis (Greek for "remembering") is a persistent memory system for AI assistants. Each user has an `Anamnesis/<username>/` folder containing markdown files with YAML frontmatter that store memories created and managed through MCP tools (`create_memory`, `get_memory`, `like_memory`, etc.).

The Anamnesis folder is **read-only in the web UI** — you cannot upload, create, delete, or modify files through the browser. It is managed exclusively through MCP tools. The UI shows the contents for browsing and inspection only. Memories can be liked or disliked to influence their relevance in search results.

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

