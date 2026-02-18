"""Projects API routes."""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from ..deps import DB, CurrentUser, get_active_project
from ...db.models import Project, ProjectFolderSetting

router = APIRouter()


class ProjectResponse(BaseModel):
    id: int
    name: str
    is_default: bool


class ProjectListResponse(BaseModel):
    projects: list[ProjectResponse]
    active_project_id: int


class CreateProjectRequest(BaseModel):
    name: str


@router.get("/")
async def list_projects(user: CurrentUser, db: DB) -> ProjectListResponse:
    """List all projects for the current user."""
    project = await get_active_project(user, db)
    active_id = project.id
    result = await db.execute(
        select(Project).where(Project.user_id == user.id).order_by(Project.created_at)
    )
    projects = result.scalars().all()
    return ProjectListResponse(
        projects=[
            ProjectResponse(id=p.id, name=p.name, is_default=p.is_default) for p in projects
        ],
        active_project_id=active_id,
    )


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_project(request: CreateProjectRequest, user: CurrentUser, db: DB) -> ProjectResponse:
    """Create a new project."""
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Project name cannot be empty")

    # Check for duplicate name
    result = await db.execute(
        select(Project).where(Project.user_id == user.id, Project.name == name)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="A project with that name already exists")

    project = Project(name=name, user_id=user.id, is_default=False)
    db.add(project)
    await db.flush()
    return ProjectResponse(id=project.id, name=project.name, is_default=project.is_default)


@router.delete("/{project_id}")
async def delete_project(project_id: int, user: CurrentUser, db: DB):
    """Delete a project. Cannot delete the default project."""
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if project.is_default:
        raise HTTPException(status_code=400, detail="Cannot delete the default project")

    # If deleting the active project, switch to default
    if user.active_project_id == project_id:
        default_result = await db.execute(
            select(Project).where(Project.user_id == user.id, Project.is_default == True)  # noqa: E712
        )
        default_project = default_result.scalar_one()
        user.active_project_id = default_project.id

    await db.delete(project)
    await db.flush()
    return {"ok": True, "active_project_id": user.active_project_id}


@router.put("/{project_id}/select")
async def select_project(project_id: int, user: CurrentUser, db: DB):
    """Set the active project for the current user."""
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    user.active_project_id = project.id
    await db.flush()
    return {"ok": True, "active_project_id": project.id}
