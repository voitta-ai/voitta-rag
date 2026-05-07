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
If multiple users exist on the instance, set VOITTA_USER=<id-or-name>
to disambiguate; otherwise the first user is selected.
"""

import json
import os
import re
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


USER_FORM_RE = re.compile(
    r'action="/select-user/(?P<id>\d+)".*?<span class="user-name">(?P<name>[^<]+)</span>',
    re.DOTALL,
)


def parse_users(html):
    """Extract (id, name) tuples from the landing page user picker."""
    retval = [(int(m["id"]), m["name"].strip()) for m in USER_FORM_RE.finditer(html)]
    return retval


def pick_user(users):
    """Choose a user given the picker list, honoring VOITTA_USER (id or name)."""
    if not users:
        return None
    selector = (os.environ.get("VOITTA_USER") or "").strip()
    if selector:
        if selector.isdigit():
            sel_id = int(selector)
            for uid, name in users:
                if uid == sel_id:
                    return uid, name
        for uid, name in users:
            if name == selector:
                return uid, name
        print(f"  ERROR: VOITTA_USER='{selector}' not found in {users}")
        return None
    if len(users) == 1:
        return users[0]
    print(
        f"  ERROR: multiple users found {users}; "
        f"set VOITTA_USER=<id-or-name> to choose one"
    )
    return None


def ensure_user(session, base_url):
    """Establish a user session cookie via landing page redirect or POST select."""
    resp = session.get(f"{base_url}/", allow_redirects=False)
    if resp.status_code == 302:
        session.get(f"{base_url}{resp.headers['Location']}")
        return True
    if resp.status_code != 200:
        print(f"  ERROR: landing page returned {resp.status_code}")
        return False
    users = parse_users(resp.text)
    pick = pick_user(users)
    if pick is None:
        return False
    uid, name = pick
    print(f"  Selecting user {uid} ({name})")
    sel = session.post(f"{base_url}/select-user/{uid}", allow_redirects=False)
    if sel.status_code != 302:
        print(f"  ERROR: /select-user/{uid} returned {sel.status_code}")
        return False
    session.get(f"{base_url}{sel.headers['Location']}")
    return True


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
    """Fetch sync source config for a folder.

    Returns:
        ("ok", dict) when a sync source is configured,
        ("none", None) when the folder exists but has no sync source (API returns null),
        ("error", str)  on HTTP or decode failure.
    """
    resp = session.get(f"{base_url}/api/sync/{path}")
    if not resp.ok:
        retval = ("error", f"HTTP {resp.status_code}")
        return retval
    try:
        data = resp.json()
    except ValueError:
        retval = ("error", "non-json response")
        return retval
    if data is None:
        retval = ("none", None)
        return retval
    retval = ("ok", data)
    return retval


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
    no_source_count = 0
    error_count = 0

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
            status, source = get_sync_source(session, base_url, child_path)
            if status == "error":
                print(f"  ERROR fetching {child_path}: {source}")
                error_count += 1
                continue
            if status == "none":
                no_source_count += 1
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
            # Always preserve the source branch so the target reproduces it
            # exactly. Earlier versions dropped main/master/develop expecting
            # the importer to auto-detect, but a folder explicitly tracking
            # 'develop' could end up on 'main' when the remote has both.
            if branch:
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
    if no_source_count:
        print(f"Skipped {no_source_count} folders with no sync source")
    if error_count:
        print(f"Encountered {error_count} errors fetching sync sources -- "
              f"export may be incomplete")
    print("Note: auth tokens/usernames are NOT exported. Re-enter them on "
          "the target machine before running import_repos.py.")
    if error_count:
        sys.exit(1)


if __name__ == "__main__":
    main()
