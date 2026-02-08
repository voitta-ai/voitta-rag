"""Git sync connector using git clone/pull."""

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path

from .base import BaseSyncConnector, RemoteFile

logger = logging.getLogger(__name__)


async def _run_git_cmd(
    args: list[str],
    cwd: str | None = None,
    ssh_key: str | None = None,
    timeout: int = 30,
) -> tuple[int, str, str]:
    """Run a git command asynchronously with optional SSH key."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    key_file = None

    try:
        if ssh_key and ssh_key.strip():
            key_file = tempfile.NamedTemporaryFile(
                mode="w", suffix=".key", delete=False
            )
            key_file.write(ssh_key.strip())
            if not ssh_key.strip().endswith("\n"):
                key_file.write("\n")
            key_file.close()
            os.chmod(key_file.name, 0o600)
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {key_file.name} -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
            )
        else:
            env["GIT_SSH_COMMAND"] = (
                "ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
            )

        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, stdout.decode(), stderr.decode()
    finally:
        if key_file:
            try:
                os.unlink(key_file.name)
            except OSError:
                pass


async def list_remote_branches(repo_url: str, ssh_key: str = "") -> list[str]:
    """List branches of a remote git repo via `git ls-remote --heads`."""
    rc, stdout, stderr = await _run_git_cmd(
        ["ls-remote", "--heads", repo_url],
        ssh_key=ssh_key or None,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"git ls-remote failed: {stderr.strip()}")

    branches = []
    for line in stdout.strip().splitlines():
        # Format: "<sha>\trefs/heads/<branch>"
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            branches.append(parts[1][len("refs/heads/"):])

    # Sort with main/master first, then alphabetical
    def sort_key(b: str) -> tuple[int, str]:
        if b == "main":
            return (0, b)
        if b == "master":
            return (1, b)
        return (2, b)

    branches.sort(key=sort_key)
    return branches


class GitHubConnector(BaseSyncConnector):
    """Sync connector that uses git clone/pull for public and SSH repos."""

    async def _run_git(
        self, args: list[str], cwd: str | None = None, source=None
    ) -> tuple[int, str, str]:
        """Run a git command using the source's SSH key."""
        ssh_key = getattr(source, "gh_token", None) if source else None
        return await _run_git_cmd(args, cwd=cwd, ssh_key=ssh_key, timeout=300)

    async def list_files(self, source) -> list[RemoteFile]:
        """Not used — sync() is overridden to use git clone/pull directly."""
        return []

    async def download_file(self, source, remote_path: str, local_path: Path) -> None:
        """Not used — sync() is overridden to use git clone/pull directly."""
        pass

    async def sync(self, source, fs, keep_extensions: set[str] | None = None) -> dict:
        """Sync a git repository using clone or pull.

        - First sync: git clone
        - Subsequent syncs: git pull
        - If a subfolder (gh_path) is set, only that subfolder's contents are
          mirrored into the local folder.
        """
        folder_path = source.folder_path
        local_root = fs._resolve_path(folder_path)
        local_root.mkdir(parents=True, exist_ok=True)

        repo_url = source.gh_repo or ""
        branch = source.gh_branch or "main"
        subfolder = (source.gh_path or "").strip("/")

        if not repo_url:
            raise ValueError("Git repository URL is required")

        # We clone into a hidden .git-repo directory inside the folder
        repo_dir = local_root / ".git-repo"
        stats = {"downloaded": 0, "deleted": 0, "skipped": 0, "errors": 0}

        if (repo_dir / ".git").exists():
            # Pull updates
            logger.info("Pulling updates for %s", folder_path)

            # Fetch first
            rc, out, err = await self._run_git(
                ["fetch", "--prune", "origin"], cwd=str(repo_dir), source=source
            )
            if rc != 0:
                raise RuntimeError(f"git fetch failed: {err}")

            # Reset to origin/branch to handle force-pushes
            rc, out, err = await self._run_git(
                ["reset", "--hard", f"origin/{branch}"], cwd=str(repo_dir), source=source
            )
            if rc != 0:
                raise RuntimeError(f"git reset failed: {err}")

            # Clean untracked files
            rc, out, err = await self._run_git(
                ["clean", "-fdx"], cwd=str(repo_dir), source=source
            )
        else:
            # Fresh clone
            logger.info("Cloning %s (branch: %s) for %s", repo_url, branch, folder_path)

            # Remove repo_dir if it exists but isn't a git repo
            if repo_dir.exists():
                shutil.rmtree(repo_dir)

            clone_args = [
                "clone",
                "--single-branch",
                "--branch", branch,
                "--depth", "1",
                repo_url,
                str(repo_dir),
            ]
            rc, out, err = await self._run_git(clone_args, source=source)
            if rc != 0:
                raise RuntimeError(f"git clone failed: {err}")

        # Now mirror the repo (or subfolder) into the local folder
        source_dir = repo_dir / subfolder if subfolder else repo_dir
        if not source_dir.exists():
            raise FileNotFoundError(
                f"Subfolder '{subfolder}' not found in repository"
            )

        # Collect all files in source directory (excluding .git and hidden files)
        remote_paths: set[str] = set()
        for src_file in source_dir.rglob("*"):
            if src_file.is_dir():
                continue
            if src_file.is_symlink() and not src_file.exists():
                logger.debug("Skipping broken symlink: %s", src_file)
                continue
            rel = src_file.relative_to(source_dir)
            # Skip .git directory and hidden files
            parts = rel.parts
            if any(p.startswith(".") for p in parts):
                continue
            remote_paths.add(str(rel))

        # Copy new/changed files
        for rel_str in sorted(remote_paths):
            src_file = source_dir / rel_str
            dst_file = local_root / rel_str

            if dst_file.exists():
                # Compare by size + mtime for speed
                src_stat = src_file.stat()
                dst_stat = dst_file.stat()
                if (
                    src_stat.st_size == dst_stat.st_size
                    and src_stat.st_mtime <= dst_stat.st_mtime
                ):
                    stats["skipped"] += 1
                    continue

            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src_file), str(dst_file))
            stats["downloaded"] += 1

        # Delete local files that no longer exist in repo
        _keep = keep_extensions or set()
        for local_file in local_root.rglob("*"):
            if local_file.is_file() and not local_file.name.startswith("."):
                # Skip the .git-repo directory
                try:
                    local_file.relative_to(repo_dir)
                    continue
                except ValueError:
                    pass

                if local_file.suffix.lower() in _keep:
                    continue

                rel = str(local_file.relative_to(local_root))
                if rel not in remote_paths:
                    try:
                        local_file.unlink()
                        stats["deleted"] += 1
                        logger.info("Deleted (not in repo): %s", rel)
                    except Exception as e:
                        logger.error("Failed to delete %s: %s", rel, e)
                        stats["errors"] += 1

        # Clean up empty directories (excluding .git-repo)
        for dirpath in sorted(local_root.rglob("*"), reverse=True):
            if dirpath.is_dir() and not any(dirpath.iterdir()):
                try:
                    dirpath.relative_to(repo_dir)
                    continue
                except ValueError:
                    pass
                try:
                    dirpath.rmdir()
                except Exception:
                    pass

        logger.info("Git sync complete for %s: %s", folder_path, stats)
        return stats
