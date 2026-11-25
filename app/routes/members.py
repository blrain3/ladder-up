# 会员列表、会员证、照片、退会与除名。
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import (
    StreamingResponse,
)
from python_multipart.exceptions import MultipartParseError
from sqlalchemy import func, select, text, true, update
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from .. import routes
from ..assessment import (
    ACTIVE_STATUSES,
    AssessmentError,
    active_target,
    expel,
    withdraw,
)
from ..auth import (
    active_member,
    allows,
    audit,
    authenticated,
    current_member,
    deny,
    permission_codes,
    require_permission,
    unauthorized,
    validate_csrf,
)
from ..db import SessionLocal, get_session
from ..models import (
    Attachment,
    Department,
    Member,
    MemberApplication,
)
from ..models.base import local_time
from ..profile import (
    full_csv_sections,
    render_csv_sections,
)
from ..security import (
    load_session,
)
from ..storage import CHUNK_BYTES, MAX_UPLOAD_BYTES, stage_photo
from ..web import content_type, page, success, wants_html
from ._common import (
    SHANGHAI,
    api_error,
    bank_write,
    invalid,
    object_id,
    optional_text,
    payload,
    redirect,
    s5_target,
)

router = APIRouter()
lifecycle_router = APIRouter()


def member_data(member, department):
    return {
        "id": member.id, "student_no": member.student_no, "name": member.name,
        "major": member.major, "grade": member.grade, "member_type": member.member_type,
        "member_status": member.member_status, "dept_id": member.dept_id, "department": department,
    }


def member_scope(request, member, session, target_id=None):
    codes = permission_codes(member, session)
    if allows(codes, "member:read"):
        if member.role == "dept_admin":
            if member.dept_id is None:
                return Member.id == member.id
            return Member.dept_id == member.dept_id
        return true()
    if allows(codes, "member:read:self"):
        if target_id is not None and target_id != member.id:
            deny(request, session, target_id)
        return Member.id == member.id
    deny(request, session, target_id)


@router.get("/members")
def members(request: Request, page_number: int = Query(1, alias="page", ge=1, le=2**63 // 100),
            size: int = Query(20, ge=1, le=100), member: Member = Depends(authenticated),
            session: Session = Depends(get_session)):
    filters = (Member.deleted_at.is_(None), member_scope(request, member, session))
    total = session.scalar(select(func.count()).select_from(Member).where(*filters))
    rows = session.execute(
        select(Member, Department.name).outerjoin(Department).where(*filters)
        .order_by(Member.id).offset((page_number - 1) * size).limit(size)
    )
    data = {
        "total": total, "page": page_number, "size": size,
        "items": [member_data(item, department) for item, department in rows],
    }
    if wants_html(request):
        return page(request, "members.html", pagination=data)
    return success(data)


@router.get("/members/export")
def export_members_csv(request: Request,
                       member: Member = Depends(require_permission("system:admin", target_type="export")),
                       session: Session = Depends(get_session)):
    """全量在册会员 CSV：system:admin，五节结构逐会员重复。"""
    codes = permission_codes(member, session)
    engine = request.app.state.engine

    def stream():
        yield "\ufeff"
        with SessionLocal(bind=engine) as export_session:
            # Keep every batch on the same SQLite read snapshot.
            export_session.execute(text("BEGIN"))
            yield from render_csv_sections(full_csv_sections(export_session, member, codes))

    filename = f"members-{routes.datetime.now(SHANGHAI).strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        stream(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/members/{id}")
def member_detail(request: Request, id: int = Depends(object_id),
                  member: Member = Depends(authenticated),
                  session: Session = Depends(get_session)):
    scope = member_scope(request, member, session, id)
    row = session.execute(
        select(Member, Department.name).outerjoin(Department).where(
            Member.id == id, Member.deleted_at.is_(None), scope,
        )
    ).one_or_none()
    if row is None:
        if member.role == "dept_admin" and id != member.id:
            deny(request, session, id)
        raise HTTPException(404, "会员不存在")
    item, department = row
    data = member_data(item, department) | {
        "email": item.email, "phone": item.phone, "joined_on": item.joined_on,
        "photo_attachment_id": item.photo_attachment_id,
        "can_write": photo_access(member, item, session, write=True),
    }
    if wants_html(request):
        can_expel = (
            allows(permission_codes(member, session), "member:approve")
            and item.member_status in ACTIVE_STATUSES
            and (member.role != "dept_admin" or bool(member.dept_id and member.dept_id == item.dept_id))
        )
        return page(request, "member.html", profile=data, can_expel=can_expel)
    return success(data)


@router.get("/members/{id}/card")
def member_card(request: Request, id: int = Depends(object_id),
                member: Member = Depends(authenticated),
                session: Session = Depends(get_session)):
    scope = member_scope(request, member, session, id)
    row = session.execute(
        select(Member, Department.name).outerjoin(Department).where(
            Member.id == id, Member.deleted_at.is_(None), scope,
        )
    ).one_or_none()
    if row is None:
        if member.role == "dept_admin" and id != member.id:
            deny(request, session, id)
        raise HTTPException(404, "会员不存在")
    item, department = row
    data = member_data(item, department) | {
        "joined_on": item.joined_on,
        "photo_attachment_id": item.photo_attachment_id,
    }
    return page(request, "card.html", profile=data)


def photo_access(actor, target, session, *, write=False):
    if target is None or target.deleted_at is not None:
        return False
    if actor.id == target.id or actor.role in {"coach", "sys_admin"}:
        return True
    if actor.role == "dept_admin":
        return actor.dept_id is not None and actor.dept_id == target.dept_id
    return not write and allows(permission_codes(actor, session), "member:read")


def photo_write_target(request, session, actor, target_id):
    target = session.get(Member, target_id)
    if target is None or target.deleted_at is not None:
        raise HTTPException(404, "会员不存在")
    if not photo_access(actor, target, session, write=True):
        deny(request, session, target_id, target_type="member")
    return target


class PhotoMultipartParser(MultiPartParser):
    def on_part_begin(self):
        super().on_part_begin()
        self.file_bytes = 0

    def on_part_data(self, data, start, end):
        if self._current_part.file is not None:
            self.file_bytes += end - start
            if self.file_bytes > MAX_UPLOAD_BYTES:
                raise MultiPartException("文件不能超过 5 MB")
        super().on_part_data(data, start, end)


async def photo_form(request: Request, member: Member = Depends(current_member),
                     session: Session = Depends(get_session)):
    if content_type(request) != "multipart/form-data":
        raise invalid("请上传 JPEG 或 PNG 图片")

    async def bounded_body():
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES + 64 * 1024:
                raise MultiPartException("上传请求过大")
            for offset in range(0, len(chunk), CHUNK_BYTES):
                yield chunk[offset:offset + CHUNK_BYTES]

    parser = PhotoMultipartParser(
        request.headers, bounded_body(), max_files=1, max_fields=1, max_part_size=1024,
    )
    try:
        form = await parser.parse()
    except (MultiPartException, MultipartParseError):
        raise invalid("上传内容无效或超过大小限制") from None
    try:
        await validate_csrf(request, session, form)
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise invalid("请上传 JPEG 或 PNG 图片")
        yield member, file
    finally:
        await form.close()


@router.post("/members/{id}/photo")
def upload_photo(request: Request, id: int = Depends(object_id),
                 upload=Depends(photo_form),
                 session: Session = Depends(get_session)):
    member, file = upload
    actor_id = member.id
    photo_write_target(request, session, member, id)
    session.rollback()
    with stage_photo(file, id, request.app.state.uploads_dir) as (attachment, staging, final_path):
        session.execute(text("BEGIN IMMEDIATE"))
        actor = session.get(Member, actor_id)
        if not active_member(actor) or not load_session(request.cookies["sid"], session=session):
            raise unauthorized()
        target = photo_write_target(request, session, actor, id)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, final_path)
        session.add(attachment)
        session.flush()
        if target.photo_attachment_id is not None:
            previous = session.get(Attachment, target.photo_attachment_id)
            if previous is not None and previous.deleted_at is None:
                previous.deleted_at = local_time()
        target.photo_attachment_id = attachment.id
        target.updated_at = local_time()
        audit(request, session, "member.photo.uploaded", actor_id=actor_id, target_id=id)
        session.commit()
    if wants_html(request):
        return redirect(f"/members/{id}")
    return success({"attachmentId": attachment.id})


@router.post("/members/{id}/photo/delete")
def delete_photo(request: Request, id: int = Depends(object_id),
                 member: Member = Depends(authenticated),
                 session: Session = Depends(get_session)):
    actor_id = member.id
    session.rollback()
    session.execute(text("BEGIN IMMEDIATE"))
    member = session.get(Member, actor_id)
    if not active_member(member) or not load_session(request.cookies["sid"], session=session):
        raise unauthorized()
    target = photo_write_target(request, session, member, id)
    if target.photo_attachment_id is None:
        raise HTTPException(400, {"code": 40002, "message": "该会员没有照片"})
    attachment = session.get(Attachment, target.photo_attachment_id)
    if attachment is not None and attachment.deleted_at is None:
        attachment.deleted_at = local_time()
    target.photo_attachment_id = None
    target.updated_at = local_time()
    audit(request, session, "member.photo.deleted", actor_id=member.id, target_id=target.id)
    session.commit()
    if wants_html(request):
        return redirect(f"/members/{id}")
    return success({"deleted": True})


MEMBER_APPROVER = require_permission("member:approve", target_type="member")


def close_pending_leave(session, member_id, reviewer_id, status, note):
    result = session.execute(update(MemberApplication).where(
        MemberApplication.member_id == member_id, MemberApplication.apply_type == "退会",
        MemberApplication.status == "待审核", MemberApplication.active_flag == "1",
    ).values(status=status, active_flag=None, review_note=note,
             reviewed_by=reviewer_id, reviewed_at=local_time()))
    return result.rowcount


@lifecycle_router.post("/members/{id}/leave")
def request_leave(request: Request, id: int = Depends(object_id), data=Depends(payload),
                  member: Member = Depends(authenticated), session: Session = Depends(get_session)):
    if member.id != id:
        deny(request, session, id)
    with bank_write(session, "已有待审核的退会申请", legacy_busy_response=False):
        try:
            session.refresh(member)
            active_target(session, id, action="退会")
        except AssessmentError as exc:
            raise api_error(exc.status, exc.code, str(exc)) from None
        application = MemberApplication(
            member_id=id, student_no=member.student_no, name=member.name, apply_type="退会",
            status="待审核", active_flag="1", created_at=local_time(),
        )
        session.add(application)
        session.flush()
        audit(request, session, "member.leave.requested", actor_id=id, target_id=id,
              detail={"application_id": application.id, "reason": optional_text(data, "reason", 500)})
    return success({"id": id, "application_id": application.id, "status": "待审核",
                    "member_status": member.member_status})


@lifecycle_router.post("/members/{id}/leave/approve")
def approve_leave(request: Request, id: int = Depends(object_id), data=Depends(payload),
                  member: Member = Depends(MEMBER_APPROVER), session: Session = Depends(get_session)):
    if member.id == id:
        deny(request, session, id)
    decision = data.get("decision", "approve")
    if decision not in ("approve", "reject"):
        raise api_error(400, 40001, "审批决定无效")
    status = "已通过" if decision == "approve" else "已拒绝"
    note = optional_text(data, "reason", 500)
    with bank_write(session, "退会审批冲突", legacy_busy_response=False):
        s5_target(session, member, id)
        try:
            target = active_target(session, id, action="退会")
            if close_pending_leave(session, id, member.id, status, note) != 1:
                raise api_error(404, 40401, "待审核退会申请不存在")
            if decision == "approve":
                withdraw(session, id, member.id, note, action="member.leave.approved")
            else:
                audit(request, session, "member.leave.rejected", actor_id=member.id, target_id=id,
                      detail={"reason": note})
        except AssessmentError as exc:
            raise api_error(exc.status, exc.code, str(exc)) from None
    if wants_html(request):
        return page(request, "member_left.html", title=status,
                    message="该会员已退会，其会话已全部撤销" if decision == "approve" else "退会申请已拒绝")
    return success({"id": id, "status": status, "member_status": target.member_status})


@lifecycle_router.post("/members/{id}/withdraw")
def withdraw_member(request: Request, id: int = Depends(object_id), data=Depends(payload),
                    member: Member = Depends(MEMBER_APPROVER),
                    session: Session = Depends(get_session)):
    if member.id == id:
        deny(request, session, id, "member")
    with bank_write(session, "退会冲突", legacy_busy_response=False):
        reason = optional_text(data, "reason", 500)
        try:
            s5_target(session, member, id)
            withdraw(session, id, member.id, reason)
            close_pending_leave(session, id, member.id, "已通过", reason)
        except AssessmentError as exc:
            raise api_error(exc.status, exc.code, str(exc)) from None
    if wants_html(request):
        return page(request, "member_left.html", title="退会成功", message="已退出，需重新登录")
    return success({"id": id, "member_status": "已退会", "message": "已退出，需重新登录"})


@lifecycle_router.post("/members/{id}/expel")
def expel_member(request: Request, id: int = Depends(object_id), data=Depends(payload),
                 member: Member = Depends(MEMBER_APPROVER),
                 session: Session = Depends(get_session)):
    reason = data.get("reason")
    if not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 500:
        raise api_error(400, 40001, "除名事由须为 10–500 字")
    reason = reason.strip()
    with bank_write(session, "除名冲突", legacy_busy_response=False):
        try:
            s5_target(session, member, id)
            expel(session, id, member.id, reason)
            close_pending_leave(session, id, member.id, "已拒绝", "该会员已除名")
        except AssessmentError as exc:
            raise api_error(exc.status, exc.code, str(exc)) from None
    if wants_html(request):
        return page(request, "member_left.html", title="除名成功",
                    message="该会员已除名，其会话已全部撤销")
    return success({"id": id, "member_status": "已除名", "message": "已除名，需重新登录"})
