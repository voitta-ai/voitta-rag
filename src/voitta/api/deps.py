"""API dependencies."""

from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.database import get_db
from ..db.models import Project, User
from ..services.filesystem import FilesystemService
from ..services.metadata import MetadataService


async def get_current_user(
    db: Annotated[AsyncSession, Depends(get_db)],
    voitta_user_id: Annotated[int | None, Cookie()] = None,
) -> User:
    """Get the current user from cookie."""
    if voitta_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/"},
        )

    result = await db.execute(select(User).where(User.id == voitta_user_id))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/"},
        )

    return user


async def get_optional_user(
    db: Annotated[AsyncSession, Depends(get_db)],
    voitta_user_id: Annotated[int | None, Cookie()] = None,
) -> User | None:
    """Get the current user from cookie, or None if not logged in."""
    if voitta_user_id is None:
        return None

    result = await db.execute(select(User).where(User.id == voitta_user_id))
    return result.scalar_one_or_none()


def get_filesystem_service() -> FilesystemService:
    """Get filesystem service instance."""
    return FilesystemService()


async def get_metadata_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MetadataService:
    """Get metadata service instance."""
    return MetadataService(db)


async def get_active_project(user: User, db: AsyncSession) -> Project:
    """Get the user's active project, creating a default project if needed."""
    if user.active_project_id:
        result = await db.execute(
            select(Project).where(Project.id == user.active_project_id)
        )
        project = result.scalar_one_or_none()
        if project:
            return project

    # Find existing default project
    result = await db.execute(
        select(Project).where(Project.user_id == user.id, Project.is_default == True)  # noqa: E712
    )
    project = result.scalar_one_or_none()

    if not project:
        project = Project(name="Default", user_id=user.id, is_default=True)
        db.add(project)
        await db.flush()

    user.active_project_id = project.id
    await db.flush()
    return project


# Type aliases for cleaner dependency injection
CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]
Filesystem = Annotated[FilesystemService, Depends(get_filesystem_service)]
Metadata = Annotated[MetadataService, Depends(get_metadata_service)]
DB = Annotated[AsyncSession, Depends(get_db)]
