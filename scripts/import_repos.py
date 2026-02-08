#!/usr/bin/env python3
"""Bulk-import repositories into voitta-rag from a JSON config file.

For each repo in the config:
1. Create the folder (parent/repo-name) via the API.
2. Configure the sync source (github connector with auth).
3. If branch is not specified, query the remote for the default branch.
4. Trigger a sync.

Usage:
    python3 scripts/import_repos.py [path/to/config.json]

Defaults to scripts/import_repos.json if no argument given.
Reads VOITTA_HOST and VOITTA_PORT from .env (or env) to build the base URL.
"""

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent


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
        if not os.environ.get(key):
            os.environ[key] = value


def get_base_url():
    host = os.environ.get("VOITTA_HOST", "localhost")
    if host == "0.0.0.0":
        host = "localhost"
    port = os.environ.get("DOCKER_PORT", "58000")
    retval = f"http://{host}:{port}"
    return retval


def get_host(repo_url):
    """Extract hostname from git URL (SSH or HTTPS)."""
    if repo_url.startswith("git@"):
        # git@github.com:org/repo.git
        host_part = repo_url.split("@", 1)[1].split(":", 1)[0]
        retval = host_part
    else:
        retval = urlparse(repo_url).hostname
    return retval


def get_repo_name(repo_url):
    """Extract repo name from URL."""
    if repo_url.startswith("git@"):
        path = repo_url.split(":", 1)[1]
    else:
        path = urlparse(repo_url).path
    retval = path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    return retval


def ensure_user(session, base_url):
    """Ensure at least one user exists and set cookie."""
    # Hit the landing page -- if auto-login works, we get redirected to /browse
    resp = session.get(f"{base_url}/", allow_redirects=False)
    if resp.status_code == 302:
        # Follow redirect to /browse, which sets the cookie
        session.get(f"{base_url}{resp.headers['Location']}")
        return True
    # If we got a 200, we're on the landing page -- parse user IDs
    # Just POST to select the first user link
    print("  Warning: could not auto-login. Visit the UI first.")
    return False


def create_folder(session, base_url, parent, name):
    """Create a folder via the API. Returns True if created or already exists."""
    resp = session.post(
        f"{base_url}/api/folders",
        json={"path": parent, "name": name},
    )
    if resp.status_code == 409:
        # Already exists
        return True
    if resp.ok:
        return True
    print(f"  ERROR creating folder {parent}/{name}: {resp.status_code} {resp.text}")
    return False


def query_default_branch(session, base_url, repo_url, host_config):
    """Query the remote for branches and pick the default."""
    params = {"repo_url": repo_url}
    auth_method = host_config.get("auth_method", "ssh")
    if auth_method == "token":
        params["username"] = host_config.get("username", "")
        params["token"] = host_config.get("token", "")

    resp = session.get(f"{base_url}/api/sync/git/branches", params=params)
    if not resp.ok:
        print(f"  Warning: could not list branches for {repo_url}: {resp.text}")
        return "main"
    branches = resp.json().get("branches", [])
    # Prefer main, then master, then first
    for candidate in ("main", "master", "develop"):
        if candidate in branches:
            return candidate
    retval = branches[0] if branches else "main"
    return retval


def check_existing_sync(session, base_url, folder_path):
    """Check if a sync source is already configured. Returns True if exists."""
    resp = session.get(f"{base_url}/api/sync/{folder_path}")
    if resp.ok and resp.json() is not None:
        return True
    return False


def configure_sync(session, base_url, folder_path, repo_url, branch, host_config):
    """Configure github sync source for a folder."""
    auth_method = host_config.get("auth_method", "ssh")
    github_config = {
        "repo": repo_url,
        "branch": branch,
        "path": "",
        "auth_method": auth_method,
        "ssh_key": "",
        "username": "",
        "token": "",
    }
    if auth_method == "token":
        github_config["username"] = host_config.get("username", "")
        github_config["token"] = host_config.get("token", "")

    payload = {
        "source_type": "github",
        "github": github_config,
    }
    resp = session.put(f"{base_url}/api/sync/{folder_path}", json=payload)
    if not resp.ok:
        print(f"  ERROR configuring sync for {folder_path}: {resp.status_code} {resp.text}")
        return False
    return True


def enable_indexing(session, base_url, folder_path):
    """Enable indexing and search for a folder."""
    resp = session.put(
        f"{base_url}/api/settings/folders/{folder_path}",
        json={"enabled": True},
    )
    if not resp.ok:
        print(f"  Warning: could not enable indexing: {resp.status_code}")
    resp = session.put(
        f"{base_url}/api/settings/folders/{folder_path}/search-active",
        json={"search_active": True},
    )
    if not resp.ok:
        print(f"  Warning: could not enable search: {resp.status_code}")


def trigger_sync(session, base_url, folder_path):
    """Trigger sync for a folder."""
    resp = session.post(f"{base_url}/api/sync/{folder_path}/trigger")
    if not resp.ok:
        print(f"  ERROR triggering sync for {folder_path}: {resp.status_code} {resp.text}")
        return False
    return True


def wait_for_sync(session, base_url, folder_path, timeout=300):
    """Wait for sync to complete."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = session.get(f"{base_url}/api/sync/{folder_path}/status")
        except requests.ConnectionError:
            # Server may have restarted; wait and retry
            print(f"  Connection lost, waiting for server...", end="", flush=True)
            time.sleep(5)
            for _ in range(30):
                try:
                    session.get(f"{base_url}/", timeout=2)
                    break
                except requests.ConnectionError:
                    print(".", end="", flush=True)
                    time.sleep(2)
            print(" reconnected")
            continue
        if resp.ok:
            data = resp.json()
            sync_status = data.get("sync_status", "")
            if sync_status == "synced":
                return True
            if sync_status == "error":
                print(f"  Sync error: {data.get('sync_error', 'unknown')}")
                return False
        time.sleep(2)
    print(f"  Timeout waiting for sync of {folder_path}")
    return False


def main():
    load_dotenv()

    config_path = SCRIPT_DIR / "import_repos.json"
    if len(sys.argv) > 1:
        config_path = Path(sys.argv[1])

    if not config_path.exists():
        print(f"Config file not found: {config_path}")
        sys.exit(1)

    config = json.loads(config_path.read_text())
    hosts = config.get("hosts", {})
    folders = config.get("folders", {})

    base_url = get_base_url()
    print(f"voitta-rag at {base_url}")

    session = requests.Session()

    # Wait for server to be ready
    print("Waiting for server...", end="", flush=True)
    for _ in range(30):
        try:
            session.get(f"{base_url}/", timeout=2)
            break
        except requests.ConnectionError:
            print(".", end="", flush=True)
            time.sleep(2)
    else:
        print("\nServer not reachable after 60s")
        sys.exit(1)
    print(" ready")

    # Ensure we have a user session
    if not ensure_user(session, base_url):
        sys.exit(1)

    total = sum(len(repos) for repos in folders.values())
    done = 0
    synced = 0
    skipped = 0
    failed = 0

    for folder_name, repos in folders.items():
        # Create parent folder
        if not create_folder(session, base_url, "", folder_name):
            continue

        for entry in repos:
            repo_url = entry["repo"]
            repo_name = get_repo_name(repo_url)
            host = get_host(repo_url)
            host_config = hosts.get(host, {})

            done += 1
            print(f"[{done}/{total}] {folder_name}/{repo_name}")

            try:
                # Create subfolder
                if not create_folder(session, base_url, folder_name, repo_name):
                    failed += 1
                    continue

                folder_path = f"{folder_name}/{repo_name}"

                # Check if already imported
                if check_existing_sync(session, base_url, folder_path):
                    print(f"  Already imported, skipping")
                    skipped += 1
                    continue

                # Determine branch
                branch = entry.get("branch")
                if not branch:
                    branch = query_default_branch(session, base_url, repo_url, host_config)
                    print(f"  Auto-detected branch: {branch}")

                # Configure sync
                if not configure_sync(session, base_url, folder_path, repo_url, branch, host_config):
                    failed += 1
                    continue

                # Trigger sync
                if not trigger_sync(session, base_url, folder_path):
                    failed += 1
                    continue

                # Wait for sync to finish
                ok = wait_for_sync(session, base_url, folder_path)
                if ok:
                    enable_indexing(session, base_url, folder_path)
                    print(f"  Synced OK")
                    synced += 1
                else:
                    print(f"  Sync failed")
                    failed += 1
                time.sleep(1)
            except requests.ConnectionError:
                print(f"  Connection error, skipping")
                failed += 1

    print(f"\nDone. {done}/{total} repos processed: {synced} synced, {skipped} skipped, {failed} failed.")


if __name__ == "__main__":
    main()
