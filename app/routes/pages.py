# 首页与会员照片附件下载。

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import (
    FileResponse,
)
from sqlalchemy.orm import Session

from ..auth import (
    authenticated,
    deny,
)
from ..db import get_session
from ..models import (
    Attachment,
    Member,
)
from ..storage import resolve_upload_path
from ._common import object_id, redirect
from .members import photo_access

router = APIRouter()
home_router = APIRouter()


@home_router.get("/")
def home(_member: Member = Depends(authenticated)):
    return redirect("/members")


@router.get("/files/{id}")
def download_file(request: Request, id: int = Depends(object_id),
                  member: Member = Depends(authenticated),
                  session: Session = Depends(get_session)):
    attachment = session.get(Attachment, id)
    if attachment is None or attachment.deleted_at is not None:
        raise HTTPException(404, "附件不存在")
    owner = session.get(Member, attachment.owner_member_id) if attachment.owner_member_id else None
    if attachment.biz_type != "member_photo" or not photo_access(member, owner, session):
        deny(request, session, id, target_type="attachment")
    try:
        path = resolve_upload_path(attachment.object_key, request.app.state.uploads_dir)
    except ValueError:
        raise HTTPException(404, "附件不存在") from None
    if not path.is_file():
        raise HTTPException(404, "附件不存在")
    response = FileResponse(path, media_type=attachment.mime)
    response.headers["Content-Disposition"] = "inline"
    response.headers["Cache-Control"] = "private, max-age=60"
    return response
