#!/usr/bin/env python3
"""Export a bulk-import JSON config from a running voitta-rag instance.

Inverse of scripts/import_repos.py. Walks the 2-level folder structure
(parent/repo-name) via the API, finds subfolders with github sync sources,
and writes a JSON config file in the same format consumed by
import_repos.py.

Only github source types are exported. Secrets (username/token) are NEVER
written to the output -- only auth_method is recorded per host. Re-enter
credentials on the target machine before running import_repos.py there.

Usage:
    python3 scripts/export_repos.py [output_path]

Defaults to scripts/import_repos_personal.json (which is gitignored).
Reads VOITTA_HOST and DOCKER_PORT from .env (or env) to build the base URL.
"""

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

SKIP_TOP_LEVEL = {"Anamnesis"}


def load_dotenv():
    """Minimal .env loader -- just KEY=VALUE lines."""
    env_file = PROJECT_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip inline comments (but not inside quoted values)
        if value and value[0] not in ('"', "'"):
            value = value.split("#")[0].strip()
        if not os.environ.get(key):
            os.environ[key] = value


def get_base_url():
    host = os.environ.get("VOITTA_HOST", "localhost")
    if host == "0.0.0.0":
        host = "localhost"
    port = os.environ.get("DOCKER_PORT", "58000")
    retval = f"http://{host}:{port}"
    return retval


def get_host_from_repo(repo_url):
    """Extract hostname from git URL (SSH or HTTPS)."""
    if repo_url.startswith("git@"):
        retval = repo_url.split("@", 1)[1].split(":", 1)[0]
    else:
        retval = urlparse(repo_url).hostname
    return retval


def ensure_user(session, base_url):
    """Establish a user session cookie via landing page redirect."""
    resp = session.get(f"{base_url}/", allow_redirects=False)
    if resp.status_code == 302:
        session.get(f"{base_url}{resp.headers['Location']}")
        return True
    print("  Warning: could not auto-login. Visit the UI first.")
    return False


def list_folder(session, base_url, path):
    """List items in a folder via API. Returns list of dicts, or None on error."""
    if path:
        url = f"{base_url}/api/folders/{path}"
    else:
        url = f"{base_url}/api/folders"
    resp = session.get(url)
    if not resp.ok:
        print(f"  Warning: failed to list folder '{path or '/'}': {resp.status_code}")
        return None
    data = resp.json()
    retval = data.get("items", [])
    return retval


def get_sync_source(session, base_url, path):
    """Fetch sync source config for a folder. Returns dict or None if unset."""
    resp = session.get(f"{base_url}/api/sync/{path}")
    if not resp.ok:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data


def main():
    load_dotenv()

    output_path = PROJECT_DIR / "scripts" / "import_repos_personal.json"
    if len(sys.argv) > 1:
        output_path = Path(sys.argv[1])

    base_url = get_base_url()
    print(f"voitta-rag at {base_url}")

    session = requests.Session()

    try:
        session.get(f"{base_url}/", timeout=5)
    except requests.ConnectionError:
        print(f"Server not reachable at {base_url}")
        sys.exit(1)

    if not ensure_user(session, base_url):
        sys.exit(1)

    top_items = list_folder(session, base_url, "")
    if top_items is None:
        print("Failed to list root folder")
        sys.exit(1)

    hosts = {}
    folders = {}
    github_count = 0
    non_github_count = 0

    for top in top_items:
        if not top.get("is_dir"):
            continue
        top_name = top["name"]
        if top_name in SKIP_TOP_LEVEL:
            continue

        children = list_folder(session, base_url, top_name)
        if children is None:
            continue

        for child in children:
            if not child.get("is_dir"):
                continue
            child_path = f"{top_name}/{child['name']}"
            source = get_sync_source(session, base_url, child_path)
            if source is None:
                continue
            source_type = source.get("source_type", "")
            if source_type != "github":
                non_github_count += 1
                continue

            github = source.get("github") or {}
            repo_url = github.get("repo")
            if not repo_url:
                continue
            branch = (github.get("branch") or "").strip()
            auth_method = github.get("auth_method") or "ssh"

            host = get_host_from_repo(repo_url)
            if host and host not in hosts:
                hosts[host] = {"auth_method": auth_method}

            folders.setdefault(top_name, [])
            entry = {"repo": repo_url}
            # Only record branch if it's not one the import script would auto-detect
            if branch and branch not in ("main", "master", "develop"):
                entry["branch"] = branch
            folders[top_name].append(entry)
            github_count += 1
            print(f"  {child_path} -> {repo_url}")

    output = {"hosts": hosts, "folders": folders}

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=4) + "\n")

    print(f"\nExported {github_count} github repos across {len(folders)} "
          f"folder(s) to {output_path}")
    if non_github_count:
        print(f"Skipped {non_github_count} non-github sync sources")
    print("Note: auth tokens/usernames are NOT exported. Re-enter them on "
          "the target machine before running import_repos.py.")


if __name__ == "__main__":
    main()
