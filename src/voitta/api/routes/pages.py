"""HTML page routes."""

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from ..deps import DB, CurrentUser, Filesystem, Metadata, OptionalUser
from ...db.models import FolderIndexStatus, User, UserFolderSetting

router = APIRouter()


def get_templates(request: Request):
    """Get Jinja2 templates from app state."""
    return request.app.state.templates


@router.get("/", response_class=HTMLResponse)
async def landing_page(
    request: Request,
    db: DB,
    user: OptionalUser,
):
    """Landing page with user selection."""
    # If already logged in, redirect to browser
    if user is not None:
        return RedirectResponse(url="/browse", status_code=302)

    # Get all users
    result = await db.execute(select(User).order_by(User.name))
    users = result.scalars().all()

    templates = get_templates(request)
    return templates.TemplateResponse(
        request,
        "landing.html",
        {"users": users},
    )


@router.post("/select-user/{user_id}")
async def select_user(user_id: int, db: DB):
    """Select a user and set cookie."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        return RedirectResponse(url="/", status_code=302)

    response = RedirectResponse(url="/browse", status_code=302)
    response.set_cookie(
        key="voitta_user_id",
        value=str(user.id),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 365,  # 1 year
    )
    return response


@router.get("/logout")
async def logout():
    """Log out and clear cookie."""
    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie("voitta_user_id")
    return response


@router.get("/browse", response_class=HTMLResponse)
@router.get("/browse/{path:path}", response_class=HTMLResponse)
async def browse(
    request: Request,
    user: CurrentUser,
    fs: Filesystem,
    metadata_svc: Metadata,
    db: DB,
    path: str = "",
):
    """File browser page."""
    try:
        items = fs.list_directory(path)
        breadcrumbs = fs.get_breadcrumbs(path)
        current_info = fs.get_info(path) if path else None
    except FileNotFoundError:
        return RedirectResponse(url="/browse", status_code=302)
    except NotADirectoryError:
        # If it's a file, redirect to parent
        parent = "/".join(path.split("/")[:-1])
        return RedirectResponse(url=f"/browse/{parent}", status_code=302)

    # Get metadata for current path
    current_metadata = None
    metadata_user = None
    if path:
        current_metadata, metadata_user = await metadata_svc.get_metadata_with_user(path)

    # Get folder enabled status for current user
    folder_enabled = False
    if path and fs.is_dir(path):
        result = await db.execute(
            select(UserFolderSetting).where(
                UserFolderSetting.user_id == user.id,
                UserFolderSetting.folder_path == path,
            )
        )
        setting = result.scalar_one_or_none()
        folder_enabled = setting.enabled if setting else False

    # Get index status for all folders in the listing
    folder_paths = [item.path for item in items if item.is_dir]
    index_statuses = {}
    if folder_paths:
        result = await db.execute(
            select(FolderIndexStatus).where(FolderIndexStatus.folder_path.in_(folder_paths))
        )
        for status in result.scalars().all():
            index_statuses[status.folder_path] = status.status

    # Also get current folder's index status
    current_index_status = None
    if path and fs.is_dir(path):
        result = await db.execute(
            select(FolderIndexStatus).where(FolderIndexStatus.folder_path == path)
        )
        status_row = result.scalar_one_or_none()
        current_index_status = status_row.status if status_row else "none"

    templates = get_templates(request)
    return templates.TemplateResponse(
        request,
        "browser.html",
        {
            "user": user,
            "items": items,
            "breadcrumbs": breadcrumbs,
            "current_path": path,
            "current_info": current_info,
            "current_metadata": current_metadata,
            "metadata_user": metadata_user,
            "folder_enabled": folder_enabled,
            "index_statuses": index_statuses,
            "current_index_status": current_index_status,
        },
    )
