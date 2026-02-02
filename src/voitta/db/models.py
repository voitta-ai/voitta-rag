"""SQLAlchemy models."""

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    """Return current UTC time."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Base class for all models."""

    pass


class User(Base):
    """User model - simple user identification."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    # Relationships
    folder_settings: Mapped[list["UserFolderSetting"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    metadata_updates: Mapped[list["FileMetadata"]] = relationship(back_populates="updated_by_user")


class FileMetadata(Base):
    """Global metadata for files and folders."""

    __tablename__ = "file_metadata"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(String(1000), unique=True, nullable=False, index=True)
    metadata_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now
    )
    updated_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)

    # Relationships
    updated_by_user: Mapped[User | None] = relationship(back_populates="metadata_updates")


class UserFolderSetting(Base):
    """Per-user folder enable/disable settings for downstream applications."""

    __tablename__ = "user_folder_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    folder_path: Mapped[str] = mapped_column(String(1000), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    # Unique constraint
    __table_args__ = (UniqueConstraint("user_id", "folder_path", name="uq_user_folder"),)

    # Relationships
    user: Mapped[User] = relationship(back_populates="folder_settings")


class FolderIndexStatus(Base):
    """Global index status for folders."""

    __tablename__ = "folder_index_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    folder_path: Mapped[str] = mapped_column(String(1000), unique=True, nullable=False, index=True)
    # Status: none, pending, indexing, indexed, disabled, error
    # - disabled: chunks are preserved but excluded from MCP searches
    status: Mapped[str] = mapped_column(String(20), default="none")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class IndexedFile(Base):
    """Track indexed files with their content hash for change detection."""

    __tablename__ = "indexed_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_path: Mapped[str] = mapped_column(String(1000), unique=True, nullable=False, index=True)
    folder_path: Mapped[str] = mapped_column(String(1000), nullable=False, index=True)
    index_folder: Mapped[str] = mapped_column(
        String(1000), nullable=False, index=True
    )  # The folder at which indexing was triggered
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # SHA-256
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
