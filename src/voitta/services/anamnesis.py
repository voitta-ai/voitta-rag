"""Anamnesis — per-user memory file operations.

Each memory is a self-contained .md file with YAML-style frontmatter,
stored at <root_path>/Anamnesis/<username>/<uuid>.md.
"""

from datetime import datetime, timezone
from pathlib import Path

from ..config import get_settings

ANAMNESIS_DIR = "Anamnesis"


def _user_dir(user_name: str) -> Path:
    """Absolute path to a user's Anamnesis directory."""
    return get_settings().root_path / ANAMNESIS_DIR / user_name


def _memory_path(user_name: str, memory_id: str) -> Path:
    """Absolute path to a specific memory file."""
    return _user_dir(user_name) / f"{memory_id}.md"


def _memory_rel_path(user_name: str, memory_id: str) -> str:
    """Relative path (from root) for indexing / Qdrant."""
    return f"{ANAMNESIS_DIR}/{user_name}/{memory_id}.md"


def _anamnesis_folder_path(user_name: str) -> str:
    """Relative folder path for FolderIndexStatus."""
    return f"{ANAMNESIS_DIR}/{user_name}"


def serialize_memory(
    memory_id: str,
    content: str,
    created_at: datetime,
    modified_at: datetime,
    likes: int,
    dislikes: int,
) -> str:
    """Produce the full file content (frontmatter + body)."""
    return (
        f"---\n"
        f"memory_id: {memory_id}\n"
        f"created_at: {created_at.isoformat()}\n"
        f"modified_at: {modified_at.isoformat()}\n"
        f"likes: {likes}\n"
        f"dislikes: {dislikes}\n"
        f"---\n"
        f"{content}\n"
    )


def parse_memory(file_content: str) -> dict:
    """Parse frontmatter + body from a memory file.

    Returns dict with keys: memory_id, created_at, modified_at, likes, dislikes, content.
    """
    lines = file_content.split("\n")
    if not lines or lines[0].strip() != "---":
        return {"content": file_content}

    # Find closing ---
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        return {"content": file_content}

    # Parse frontmatter key: value pairs
    meta: dict = {}
    for line in lines[1:end_idx]:
        colon = line.find(":")
        if colon < 0:
            continue
        key = line[:colon].strip()
        value = line[colon + 1 :].strip()
        meta[key] = value

    # Body is everything after the closing ---
    body = "\n".join(lines[end_idx + 1 :]).strip()

    return {
        "memory_id": meta.get("memory_id", ""),
        "created_at": meta.get("created_at", ""),
        "modified_at": meta.get("modified_at", ""),
        "likes": int(meta.get("likes", 0)),
        "dislikes": int(meta.get("dislikes", 0)),
        "content": body,
    }


def read_memory(user_name: str, memory_id: str) -> dict:
    """Read and parse a memory file. Raises FileNotFoundError if missing."""
    path = _memory_path(user_name, memory_id)
    if not path.exists():
        raise FileNotFoundError(f"Memory not found: {memory_id}")
    return parse_memory(path.read_text(encoding="utf-8"))


def write_memory(
    user_name: str,
    memory_id: str,
    content: str,
    created_at: datetime,
    modified_at: datetime,
    likes: int,
    dislikes: int,
) -> Path:
    """Write a memory file, creating directories as needed."""
    path = _memory_path(user_name, memory_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        serialize_memory(memory_id, content, created_at, modified_at, likes, dislikes),
        encoding="utf-8",
    )
    return path


def delete_memory_file(user_name: str, memory_id: str) -> bool:
    """Delete a memory file. Returns True if it existed."""
    path = _memory_path(user_name, memory_id)
    if path.exists():
        path.unlink()
        return True
    return False


def list_user_memories(user_name: str) -> list[dict]:
    """List all memories for a user (glob *.md, parse each)."""
    user_path = _user_dir(user_name)
    if not user_path.exists():
        return []
    memories = []
    for md_file in sorted(user_path.glob("*.md")):
        try:
            data = parse_memory(md_file.read_text(encoding="utf-8"))
            memories.append(data)
        except Exception:
            continue
    return memories
