"""Metadata service for files and folders."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import FileMetadata, User


class MetadataService:
    """Service for managing file/folder metadata."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_metadata(self, path: str) -> str | None:
        """Get metadata for a path."""
        result = await self.db.execute(
            select(FileMetadata).where(FileMetadata.path == path)
        )
        metadata = result.scalar_one_or_none()
        return metadata.metadata_text if metadata else None

    async def set_metadata(self, path: str, text: str, user_id: int) -> FileMetadata:
        """Set or update metadata for a path."""
        result = await self.db.execute(
            select(FileMetadata).where(FileMetadata.path == path)
        )
        metadata = result.scalar_one_or_none()

        if metadata:
            metadata.metadata_text = text
            metadata.updated_by = user_id
        else:
            metadata = FileMetadata(
                path=path,
                metadata_text=text,
                updated_by=user_id,
            )
            self.db.add(metadata)

        await self.db.flush()
        return metadata

    async def get_metadata_with_user(self, path: str) -> tuple[str | None, str | None]:
        """Get metadata with the user who last updated it."""
        result = await self.db.execute(
            select(FileMetadata, User)
            .outerjoin(User, FileMetadata.updated_by == User.id)
            .where(FileMetadata.path == path)
        )
        row = result.first()
        if row:
            metadata, user = row
            return metadata.metadata_text, user.name if user else None
        return None, None

    async def delete_metadata(self, path: str) -> bool:
        """Delete metadata for a path."""
        result = await self.db.execute(
            select(FileMetadata).where(FileMetadata.path == path)
        )
        metadata = result.scalar_one_or_none()
        if metadata:
            await self.db.delete(metadata)
            return True
        return False
