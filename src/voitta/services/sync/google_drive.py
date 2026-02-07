"""Google Drive sync connector."""

import asyncio
import json
from pathlib import Path

from .base import BaseSyncConnector, RemoteFile


class GoogleDriveConnector(BaseSyncConnector):

    def _get_service(self, source):
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        creds_info = json.loads(source.gd_service_account_json)
        creds = Credentials.from_service_account_info(
            creds_info, scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )
        return build("drive", "v3", credentials=creds)

    async def list_files(self, source) -> list[RemoteFile]:
        service = self._get_service(source)
        files: list[RemoteFile] = []
        await asyncio.to_thread(
            self._list_recursive_sync, service, source.gd_folder_id, "", files
        )
        return files

    def _list_recursive_sync(self, service, folder_id, current_path, files):
        page_token = None
        while True:
            results = (
                service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id, name, mimeType, size, modifiedTime, md5Checksum)",
                    pageSize=1000,
                    pageToken=page_token,
                )
                .execute()
            )

            for item in results.get("files", []):
                item_path = f"{current_path}/{item['name']}" if current_path else item["name"]
                if item["mimeType"] == "application/vnd.google-apps.folder":
                    self._list_recursive_sync(service, item["id"], item_path, files)
                else:
                    files.append(
                        RemoteFile(
                            remote_path=item_path,
                            size=int(item.get("size", 0)),
                            modified_at=item.get("modifiedTime", ""),
                            content_hash=item.get("md5Checksum"),
                        )
                    )

            page_token = results.get("nextPageToken")
            if not page_token:
                break

    async def download_file(self, source, remote_path: str, local_path: Path) -> None:
        service = self._get_service(source)
        file_id = await asyncio.to_thread(
            self._resolve_file_id, service, source.gd_folder_id, remote_path
        )

        def _download():
            from googleapiclient.http import MediaIoBaseDownload

            request = service.files().get_media(fileId=file_id)
            with open(local_path, "wb") as f:
                downloader = MediaIoBaseDownload(f, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()

        await asyncio.to_thread(_download)

    def _resolve_file_id(self, service, root_folder_id, remote_path):
        """Walk path segments to find the file's Drive ID."""
        parts = remote_path.split("/")
        current_parent = root_folder_id
        for part in parts:
            escaped = part.replace("'", "\\'")
            results = (
                service.files()
                .list(
                    q=f"'{current_parent}' in parents and name = '{escaped}' and trashed = false",
                    fields="files(id, mimeType)",
                    pageSize=1,
                )
                .execute()
            )
            found = results.get("files", [])
            if not found:
                raise FileNotFoundError(f"Remote path not found: {remote_path}")
            current_parent = found[0]["id"]
        return current_parent
