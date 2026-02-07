"""GitHub sync connector using REST API."""

from pathlib import Path

import httpx

from .base import BaseSyncConnector, RemoteFile


class GitHubConnector(BaseSyncConnector):

    def _headers(self, source) -> dict:
        return {
            "Authorization": f"Bearer {source.gh_token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def list_files(self, source) -> list[RemoteFile]:
        owner, repo = source.gh_repo.split("/", 1)
        branch = source.gh_branch or "main"
        prefix = source.gh_path.strip("/") if source.gh_path else ""

        files = []
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}",
                params={"recursive": "1"},
                headers=self._headers(source),
            )
            resp.raise_for_status()
            tree = resp.json()

            for item in tree.get("tree", []):
                if item["type"] != "blob":
                    continue
                path = item["path"]
                if prefix and not path.startswith(prefix + "/") and path != prefix:
                    continue
                rel_path = path[len(prefix):].lstrip("/") if prefix else path
                if not rel_path:
                    continue

                files.append(
                    RemoteFile(
                        remote_path=rel_path,
                        size=item.get("size", 0),
                        modified_at="",
                        content_hash=item.get("sha"),
                    )
                )

        return files

    async def download_file(self, source, remote_path: str, local_path: Path) -> None:
        owner, repo = source.gh_repo.split("/", 1)
        branch = source.gh_branch or "main"
        prefix = source.gh_path.strip("/") if source.gh_path else ""
        full_path = f"{prefix}/{remote_path}" if prefix else remote_path

        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(
                f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{full_path}",
                headers={"Authorization": f"Bearer {source.gh_token}"},
            )
            resp.raise_for_status()
            local_path.write_bytes(resp.content)
