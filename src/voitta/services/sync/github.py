"""Git sync connector using git clone/pull."""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import httpx

from .base import BaseSyncConnector, RemoteFile

logger = logging.getLogger(__name__)


def _inject_token_into_url(repo_url: str, username: str, token: str) -> str:
    """Rewrite an HTTPS repo URL to embed credentials.

    https://github.com/org/repo  ->  https://user:token@github.com/org/repo
    """
    parsed = urlparse(repo_url)
    retval = urlunparse(parsed._replace(
        netloc=f"{username}:{token}@{parsed.hostname}"
        + (f":{parsed.port}" if parsed.port else "")
    ))
    return retval


async def _run_git_cmd(
    args: list[str],
    cwd: str | None = None,
    ssh_key: str | None = None,
    token: str | None = None,
    username: str | None = None,
    timeout: int = 30,
) -> tuple[int, str, str]:
    """Run a git command asynchronously with optional SSH key or HTTPS token."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    key_file = None
    askpass_file = None

    try:
        if token and token.strip():
            # HTTPS token auth via GIT_ASKPASS
            user = (username or "x-access-token").strip()
            askpass_file = tempfile.NamedTemporaryFile(
                mode="w", suffix=".sh", delete=False
            )
            askpass_file.write(f"#!/bin/sh\necho '{token.strip()}'\n")
            askpass_file.close()
            os.chmod(askpass_file.name, stat.S_IRWXU)
            env["GIT_ASKPASS"] = askpass_file.name
            # Also inject credentials into the URL for commands that take a URL arg
            args = list(args)
            for i, arg in enumerate(args):
                if arg.startswith("https://"):
                    args[i] = _inject_token_into_url(arg, user, token.strip())
        elif ssh_key and ssh_key.strip():
            key_file = tempfile.NamedTemporaryFile(
                mode="w", suffix=".key", delete=False
            )
            key_file.write(ssh_key.strip())
            if not ssh_key.strip().endswith("\n"):
                key_file.write("\n")
            key_file.close()
            os.chmod(key_file.name, 0o600)
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {key_file.name}"
                " -F /dev/null"
                " -o StrictHostKeyChecking=accept-new"
                " -o BatchMode=yes"
            )
        else:
            env["GIT_SSH_COMMAND"] = (
                "ssh -F /dev/null"
                " -o StrictHostKeyChecking=accept-new"
                " -o BatchMode=yes"
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
        if askpass_file:
            try:
                os.unlink(askpass_file.name)
            except OSError:
                pass


async def list_remote_branches(
    repo_url: str,
    ssh_key: str = "",
    token: str = "",
    username: str = "",
) -> list[str]:
    """List branches of a remote git repo via `git ls-remote --heads`."""
    rc, stdout, stderr = await _run_git_cmd(
        ["ls-remote", "--heads", repo_url],
        ssh_key=ssh_key or None,
        token=token or None,
        username=username or None,
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


# ---------------------------------------------------------------------------
# GitHub API helpers (issues, PRs, actions)
# ---------------------------------------------------------------------------

_GH_SANITIZE_RE = re.compile(r'[<>:"/\\|?*]')


def _sanitize_gh_filename(name: str) -> str:
    """Sanitize a string for use as a filename component."""
    sanitized = _GH_SANITIZE_RE.sub("-", name)
    sanitized = re.sub(r"\s+", "-", sanitized)
    sanitized = re.sub(r"-{2,}", "-", sanitized)
    return sanitized.strip("-")[:80]


def _parse_github_repo(repo_url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from a GitHub URL. Returns None for non-GitHub hosts."""
    # SSH format: git@github.com:org/repo.git
    m = re.match(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$", repo_url)
    if m:
        return m.group(1), m.group(2)
    # HTTPS format
    parsed = urlparse(repo_url)
    if parsed.hostname not in ("github.com", "www.github.com"):
        return None
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) >= 2:
        return parts[0], parts[1].removesuffix(".git")
    return None


def _render_gh_issue_md(issue: dict, comments: list[dict]) -> str:
    """Render a GitHub issue as a structured markdown document."""
    number = issue["number"]
    title = issue.get("title", f"Issue #{number}")
    state = issue.get("state", "")
    user = (issue.get("user") or {}).get("login", "unknown")
    created = (issue.get("created_at") or "")[:10]
    updated = (issue.get("updated_at") or "")[:10]
    labels = [lb.get("name", "") for lb in (issue.get("labels") or [])]
    assignees = [a.get("login", "") for a in (issue.get("assignees") or [])]
    milestone = (issue.get("milestone") or {}).get("title", "")

    lines: list[str] = [f"# #{number} {title}\n"]
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(f"| State | {state} |")
    lines.append(f"| Author | {user} |")
    lines.append(f"| Created | {created} |")
    lines.append(f"| Updated | {updated} |")
    if labels:
        lines.append(f"| Labels | {', '.join(labels)} |")
    if assignees:
        lines.append(f"| Assignees | {', '.join(assignees)} |")
    if milestone:
        lines.append(f"| Milestone | {milestone} |")
    lines.append("")

    body = issue.get("body") or ""
    if body:
        lines.append("## Description\n")
        lines.append(body.strip())
        lines.append("")

    if comments:
        lines.append("## Comments\n")
        for c in comments:
            author = (c.get("user") or {}).get("login", "unknown")
            date = (c.get("created_at") or "")[:10]
            cbody = c.get("body") or ""
            lines.append(f"### {author} ({date})\n")
            lines.append(cbody.strip())
            lines.append("")

    return "\n".join(lines)


def _render_gh_pr_md(pr: dict, comments: list[dict]) -> str:
    """Render a GitHub pull request as a structured markdown document."""
    number = pr["number"]
    title = pr.get("title", f"PR #{number}")
    state = pr.get("state", "")
    if pr.get("merged_at"):
        state = "merged"
    elif pr.get("draft"):
        state = "draft"
    user = (pr.get("user") or {}).get("login", "unknown")
    created = (pr.get("created_at") or "")[:10]
    updated = (pr.get("updated_at") or "")[:10]
    merged = (pr.get("merged_at") or "")[:10] if pr.get("merged_at") else ""
    base_branch = (pr.get("base") or {}).get("ref", "")
    head_branch = (pr.get("head") or {}).get("ref", "")
    labels = [lb.get("name", "") for lb in (pr.get("labels") or [])]
    reviewers = [r.get("login", "") for r in (pr.get("requested_reviewers") or [])]

    lines: list[str] = [f"# #{number} {title}\n"]
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(f"| State | {state} |")
    lines.append(f"| Author | {user} |")
    if base_branch and head_branch:
        lines.append(f"| Branch | {head_branch} → {base_branch} |")
    lines.append(f"| Created | {created} |")
    lines.append(f"| Updated | {updated} |")
    if merged:
        lines.append(f"| Merged | {merged} |")
    if labels:
        lines.append(f"| Labels | {', '.join(labels)} |")
    if reviewers:
        lines.append(f"| Reviewers | {', '.join(reviewers)} |")
    lines.append("")

    body = pr.get("body") or ""
    if body:
        lines.append("## Description\n")
        lines.append(body.strip())
        lines.append("")

    if comments:
        lines.append("## Comments\n")
        for c in comments:
            author = (c.get("user") or {}).get("login", "unknown")
            date = (c.get("created_at") or "")[:10]
            cbody = c.get("body") or ""
            lines.append(f"### {author} ({date})\n")
            lines.append(cbody.strip())
            lines.append("")

    return "\n".join(lines)


def _render_gh_run_md(run: dict, jobs: list[dict]) -> str:
    """Render a GitHub Actions workflow run as a markdown document."""
    run_number = run.get("run_number", 0)
    name = run.get("name", "Workflow")
    run_status = run.get("status", "")
    conclusion = run.get("conclusion", "")
    branch = run.get("head_branch", "")
    event = run.get("event", "")
    created = run.get("created_at", "")
    updated = run.get("updated_at", "")

    lines: list[str] = [f"# Run #{run_number} — {name}\n"]
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(f"| Status | {run_status} |")
    if conclusion:
        lines.append(f"| Conclusion | {conclusion} |")
    lines.append(f"| Branch | {branch} |")
    lines.append(f"| Event | {event} |")
    lines.append(f"| Created | {created} |")
    lines.append(f"| Updated | {updated} |")
    lines.append("")

    if jobs:
        lines.append("## Jobs\n")
        for job in jobs:
            job_name = job.get("name", "job")
            job_conclusion = job.get("conclusion", job.get("status", ""))
            started = job.get("started_at", "")
            completed = job.get("completed_at", "")
            lines.append(f"### {job_name} ({job_conclusion})")
            if started:
                lines.append(f"- Started: {started}")
            if completed:
                lines.append(f"- Completed: {completed}")
            steps = job.get("steps") or []
            if steps:
                lines.append("- Steps:")
                for step in steps:
                    step_name = step.get("name", "step")
                    step_conclusion = step.get("conclusion", step.get("status", ""))
                    lines.append(f"  - {step_name}: {step_conclusion}")
            lines.append("")

    return "\n".join(lines)


class GitHubConnector(BaseSyncConnector):
    """Sync connector that uses git clone/pull for public, SSH, and token repos."""

    async def _run_git(
        self, args: list[str], cwd: str | None = None, source=None
    ) -> tuple[int, str, str]:
        """Run a git command using the source's configured auth method."""
        auth_method = getattr(source, "gh_auth_method", None) if source else None
        if auth_method == "token":
            token = getattr(source, "gh_pat", None)
            username = getattr(source, "gh_username", None)
            retval = await _run_git_cmd(
                args, cwd=cwd, token=token, username=username, timeout=300
            )
        else:
            ssh_key = getattr(source, "gh_token", None) if source else None
            retval = await _run_git_cmd(args, cwd=cwd, ssh_key=ssh_key, timeout=300)
        return retval

    async def list_files(self, source) -> list[RemoteFile]:
        """Not used — sync() is overridden to use git clone/pull directly."""
        return []

    async def download_file(self, source, remote_path: str, local_path: Path) -> None:
        """Not used — sync() is overridden to use git clone/pull directly."""
        pass

    async def _sync_single_branch(
        self,
        source,
        repo_url: str,
        branch: str,
        local_root: Path,
        subfolder: str,
        keep_extensions: set[str] | None = None,
    ) -> dict:
        """Clone/pull a single branch and mirror its files into local_root.

        Returns stats dict with downloaded/deleted/skipped/errors counts.
        """
        repo_dir = local_root / ".git-repo"
        stats = {"downloaded": 0, "deleted": 0, "skipped": 0, "errors": 0}

        if (repo_dir / ".git").exists():
            # Pull updates
            logger.info("Pulling updates for %s (branch: %s)", local_root, branch)

            rc, out, err = await self._run_git(
                ["fetch", "--prune", "origin"], cwd=str(repo_dir), source=source
            )
            if rc != 0:
                raise RuntimeError(f"git fetch failed: {err}")

            rc, out, err = await self._run_git(
                ["reset", "--hard", f"origin/{branch}"], cwd=str(repo_dir), source=source
            )
            if rc != 0:
                raise RuntimeError(f"git reset failed: {err}")

            rc, out, err = await self._run_git(
                ["clean", "-fdx"], cwd=str(repo_dir), source=source
            )
        else:
            # Fresh clone
            logger.info("Cloning %s (branch: %s) into %s", repo_url, branch, local_root)

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

        # Mirror the repo (or subfolder) into local_root
        source_dir = repo_dir / subfolder if subfolder else repo_dir
        if not source_dir.exists():
            raise FileNotFoundError(
                f"Subfolder '{subfolder}' not found in repository"
            )

        # Collect all files (excluding .git and hidden files)
        remote_paths: set[str] = set()
        for src_file in source_dir.rglob("*"):
            if src_file.is_dir():
                continue
            if src_file.is_symlink() and not src_file.exists():
                logger.debug("Skipping broken symlink: %s", src_file)
                continue
            rel = src_file.relative_to(source_dir)
            if any(p.startswith(".") for p in rel.parts):
                continue
            remote_paths.add(str(rel))

        # Copy new/changed files
        for rel_str in sorted(remote_paths):
            src_file = source_dir / rel_str
            dst_file = local_root / rel_str

            if dst_file.exists():
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

        return stats

    async def sync(self, source, fs, keep_extensions: set[str] | None = None) -> dict:
        """Sync a git repository using clone or pull.

        - First sync: git clone
        - Subsequent syncs: git pull
        - If a subfolder (gh_path) is set, only that subfolder's contents are
          mirrored into the local folder.
        - If gh_all_branches is set, all remote branches are synced into
          branches/<branch_name>/ subfolders.
        """
        folder_path = source.folder_path
        local_root = fs._resolve_path(folder_path)
        local_root.mkdir(parents=True, exist_ok=True)

        repo_url = source.gh_repo or ""
        subfolder = (source.gh_path or "").strip("/")

        if not repo_url:
            raise ValueError("Git repository URL is required")

        if getattr(source, "gh_all_branches", False):
            return await self._sync_all_branches(
                source, repo_url, local_root, subfolder, folder_path,
                keep_extensions,
            )

        branch = source.gh_branch or "main"
        stats = await self._sync_single_branch(
            source, repo_url, branch, local_root, subfolder, keep_extensions,
        )
        logger.info("Git sync complete for %s: %s", folder_path, stats)
        return stats

    async def _sync_all_branches(
        self,
        source,
        repo_url: str,
        local_root: Path,
        subfolder: str,
        folder_path: str,
        keep_extensions: set[str] | None = None,
    ) -> dict:
        """Sync all remote branches into branches/<name>/ subfolders."""
        ssh_key = getattr(source, "gh_token", None) or ""
        token = getattr(source, "gh_pat", None) or ""
        username = getattr(source, "gh_username", None) or ""

        branches = await list_remote_branches(
            repo_url, ssh_key=ssh_key, token=token, username=username,
        )
        if not branches:
            raise RuntimeError("No branches found in remote repository")

        logger.info(
            "Syncing all branches for %s: %s", folder_path,
            ", ".join(branches),
        )

        branches_dir = local_root / "branches"
        branches_dir.mkdir(parents=True, exist_ok=True)

        totals = {"downloaded": 0, "deleted": 0, "skipped": 0, "errors": 0}

        for branch in branches:
            # Sanitise branch name for filesystem (replace / with --)
            safe_name = branch.replace("/", "--")
            branch_root = branches_dir / safe_name
            branch_root.mkdir(parents=True, exist_ok=True)

            try:
                stats = await self._sync_single_branch(
                    source, repo_url, branch, branch_root, subfolder,
                    keep_extensions,
                )
                for k in totals:
                    totals[k] += stats[k]
                logger.info(
                    "Branch %s synced for %s: %s", branch, folder_path, stats,
                )
            except Exception as e:
                logger.error(
                    "Failed to sync branch %s for %s: %s", branch, folder_path, e,
                )
                totals["errors"] += 1

        # Clean up local branch folders that no longer exist remotely
        safe_names = {b.replace("/", "--") for b in branches}
        if branches_dir.exists():
            for child in list(branches_dir.iterdir()):
                if child.is_dir() and child.name not in safe_names:
                    logger.info(
                        "Removing stale branch folder: %s", child.name,
                    )
                    shutil.rmtree(child)

        # Sync issues, pull requests, and workflow runs from GitHub API
        try:
            meta_stats = await self._sync_github_metadata(
                source, repo_url, local_root,
            )
            for k in totals:
                totals[k] += meta_stats[k]
        except Exception as e:
            logger.error(
                "Failed to sync GitHub metadata for %s: %s", folder_path, e,
            )
            totals["errors"] += 1

        logger.info(
            "Git all-branches sync complete for %s: %s", folder_path, totals,
        )
        return totals

    # ------------------------------------------------------------------
    # GitHub API helpers (issues, PRs, actions)
    # ------------------------------------------------------------------

    async def _gh_api_get(
        self,
        client: httpx.AsyncClient,
        url: str,
        token: str = "",
        params: dict | None = None,
    ) -> dict | list:
        """Make a GET request to the GitHub REST API."""
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = await client.get(url, headers=headers, params=params)
        if resp.status_code in (401, 403):
            raise RuntimeError(
                f"GitHub API auth error ({resp.status_code}): {resp.text[:200]}"
            )
        if resp.status_code == 404:
            return [] if isinstance(params, dict) else {}
        if resp.status_code != 200:
            raise RuntimeError(
                f"GitHub API error ({resp.status_code}): {resp.text[:300]}"
            )
        return resp.json()

    async def _gh_api_get_pages(
        self,
        client: httpx.AsyncClient,
        url: str,
        token: str = "",
        params: dict | None = None,
        max_items: int = 500,
    ) -> list[dict]:
        """Paginate through a GitHub API list endpoint."""
        params = dict(params or {})
        params.setdefault("per_page", 100)
        params.setdefault("page", 1)
        results: list[dict] = []
        while len(results) < max_items:
            data = await self._gh_api_get(client, url, token, params)
            if not data:
                break
            if isinstance(data, list):
                results.extend(data)
                if len(data) < params["per_page"]:
                    break
            else:
                # Wrapped response (e.g. actions/runs → {workflow_runs: [...]})
                items = (
                    data.get("workflow_runs")
                    or data.get("items")
                    or []
                )
                results.extend(items)
                if data.get("total_count", 0) <= len(results):
                    break
                if len(items) < params["per_page"]:
                    break
            params["page"] += 1
        return results[:max_items]

    # ------------------------------------------------------------------
    # Metadata sync: issues, PRs, actions
    # ------------------------------------------------------------------

    async def _sync_github_metadata(
        self,
        source,
        repo_url: str,
        local_root: Path,
    ) -> dict:
        """Sync issues, PRs, and workflow runs from the GitHub API."""
        parsed = _parse_github_repo(repo_url)
        if not parsed:
            logger.info("Non-GitHub host; skipping issues/PRs/actions sync")
            return {"downloaded": 0, "deleted": 0, "skipped": 0, "errors": 0}

        owner, repo = parsed
        token = getattr(source, "gh_pat", None) or ""
        api_base = f"https://api.github.com/repos/{owner}/{repo}"

        # Sidecar file for change tracking (like Jira/Confluence pattern)
        revisions_file = local_root / ".github_revisions.json"
        old_revisions: dict[str, str] = {}
        if revisions_file.exists():
            try:
                old_revisions = json.loads(revisions_file.read_text())
            except Exception:
                pass

        new_revisions: dict[str, str] = {}
        remote_paths: set[str] = set()
        stats = {"downloaded": 0, "deleted": 0, "skipped": 0, "errors": 0}

        async with httpx.AsyncClient(timeout=30.0) as client:
            # --- Issues ---
            try:
                await self._sync_gh_issues(
                    client, api_base, token, local_root,
                    old_revisions, new_revisions, remote_paths, stats,
                )
            except Exception as e:
                logger.error("Failed to sync GitHub issues: %s", e)
                stats["errors"] += 1

            # --- Pull Requests ---
            try:
                await self._sync_gh_pull_requests(
                    client, api_base, token, local_root,
                    old_revisions, new_revisions, remote_paths, stats,
                )
            except Exception as e:
                logger.error("Failed to sync GitHub PRs: %s", e)
                stats["errors"] += 1

            # --- Actions / Workflow Runs ---
            try:
                await self._sync_gh_actions(
                    client, api_base, token, local_root,
                    old_revisions, new_revisions, remote_paths, stats,
                )
            except Exception as e:
                logger.error("Failed to sync GitHub actions: %s", e)
                stats["errors"] += 1

        # Delete stale metadata files
        for folder_name in ("issues", "pull-requests", "actions"):
            folder = local_root / folder_name
            if not folder.exists():
                continue
            for f in folder.rglob("*.md"):
                rel = str(f.relative_to(local_root))
                if rel not in remote_paths:
                    try:
                        f.unlink()
                        stats["deleted"] += 1
                    except Exception as e:
                        logger.error("Failed to delete %s: %s", rel, e)
                        stats["errors"] += 1
            # Clean up empty directories
            for d in sorted(folder.rglob("*"), reverse=True):
                if d.is_dir() and not any(d.iterdir()):
                    try:
                        d.rmdir()
                    except Exception:
                        pass

        revisions_file.write_text(json.dumps(new_revisions), encoding="utf-8")
        logger.info("GitHub metadata sync: %s", stats)
        return stats

    async def _sync_gh_issues(
        self, client, api_base, token, local_root,
        old_revisions, new_revisions, remote_paths, stats,
    ):
        """Fetch and render GitHub issues as markdown files."""
        issues = await self._gh_api_get_pages(
            client, f"{api_base}/issues", token,
            {"state": "all", "sort": "updated", "direction": "desc"},
            max_items=500,
        )
        # GitHub issues endpoint includes PRs — filter them out
        issues = [i for i in issues if "pull_request" not in i]

        for issue in issues:
            number = issue["number"]
            title = issue.get("title", f"Issue-{number}")
            updated = issue.get("updated_at", "")
            safe_title = _sanitize_gh_filename(title)
            rel_path = f"issues/{number}-{safe_title}.md"
            remote_paths.add(rel_path)

            content_hash = hashlib.sha256(updated.encode()).hexdigest()
            new_revisions[rel_path] = content_hash

            local_file = local_root / rel_path
            if local_file.exists() and old_revisions.get(rel_path) == content_hash:
                stats["skipped"] += 1
                continue

            # Fetch comments for new/changed issues
            comments: list[dict] = []
            if issue.get("comments", 0) > 0:
                try:
                    comments = await self._gh_api_get_pages(
                        client, f"{api_base}/issues/{number}/comments", token,
                        max_items=100,
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to fetch comments for issue #%d: %s", number, e,
                    )

            md = _render_gh_issue_md(issue, comments)
            local_file.parent.mkdir(parents=True, exist_ok=True)
            local_file.write_text(md, encoding="utf-8")
            stats["downloaded"] += 1

    async def _sync_gh_pull_requests(
        self, client, api_base, token, local_root,
        old_revisions, new_revisions, remote_paths, stats,
    ):
        """Fetch and render GitHub pull requests as markdown files."""
        prs = await self._gh_api_get_pages(
            client, f"{api_base}/pulls", token,
            {"state": "all", "sort": "updated", "direction": "desc"},
            max_items=500,
        )

        for pr in prs:
            number = pr["number"]
            title = pr.get("title", f"PR-{number}")
            updated = pr.get("updated_at", "")
            safe_title = _sanitize_gh_filename(title)
            rel_path = f"pull-requests/{number}-{safe_title}.md"
            remote_paths.add(rel_path)

            content_hash = hashlib.sha256(updated.encode()).hexdigest()
            new_revisions[rel_path] = content_hash

            local_file = local_root / rel_path
            if local_file.exists() and old_revisions.get(rel_path) == content_hash:
                stats["skipped"] += 1
                continue

            # Fetch issue comments + review comments, merge chronologically
            comments: list[dict] = []
            try:
                issue_comments = await self._gh_api_get_pages(
                    client, f"{api_base}/issues/{number}/comments", token,
                    max_items=100,
                )
                review_comments = await self._gh_api_get_pages(
                    client, f"{api_base}/pulls/{number}/comments", token,
                    max_items=100,
                )
                all_comments = issue_comments + review_comments
                all_comments.sort(key=lambda c: c.get("created_at", ""))
                comments = all_comments
            except Exception as e:
                logger.warning(
                    "Failed to fetch comments for PR #%d: %s", number, e,
                )

            md = _render_gh_pr_md(pr, comments)
            local_file.parent.mkdir(parents=True, exist_ok=True)
            local_file.write_text(md, encoding="utf-8")
            stats["downloaded"] += 1

    async def _sync_gh_actions(
        self, client, api_base, token, local_root,
        old_revisions, new_revisions, remote_paths, stats,
    ):
        """Fetch and render recent GitHub Actions workflow runs."""
        runs_data = await self._gh_api_get(
            client, f"{api_base}/actions/runs", token,
            {"per_page": 100},
        )
        runs = (
            runs_data.get("workflow_runs", [])
            if isinstance(runs_data, dict) else []
        )

        for run in runs[:100]:
            run_number = run.get("run_number", 0)
            workflow_name = run.get("name", "workflow")
            updated = run.get("updated_at", "")
            safe_workflow = _sanitize_gh_filename(workflow_name)
            rel_path = f"actions/{safe_workflow}/{run_number}.md"
            remote_paths.add(rel_path)

            content_hash = hashlib.sha256(updated.encode()).hexdigest()
            new_revisions[rel_path] = content_hash

            local_file = local_root / rel_path
            if local_file.exists() and old_revisions.get(rel_path) == content_hash:
                stats["skipped"] += 1
                continue

            # Fetch job details for this run
            jobs: list[dict] = []
            try:
                jobs_data = await self._gh_api_get(
                    client,
                    f"{api_base}/actions/runs/{run['id']}/jobs",
                    token,
                )
                jobs = (
                    jobs_data.get("jobs", [])
                    if isinstance(jobs_data, dict) else []
                )
            except Exception as e:
                logger.warning(
                    "Failed to fetch jobs for run #%d: %s", run_number, e,
                )

            md = _render_gh_run_md(run, jobs)
            local_file.parent.mkdir(parents=True, exist_ok=True)
            local_file.write_text(md, encoding="utf-8")
            stats["downloaded"] += 1
