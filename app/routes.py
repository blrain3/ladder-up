import os
import re
import secrets
import sqlite3
from calendar import monthrange
from contextlib import contextmanager
from datetime import date, datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from python_multipart.exceptions import MultipartParseError
from sqlalchemy import delete, false, func, inspect, or_, select, text, true, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from bin.job import JobFailed, run_job
from bin.job import assess_term as run_assess_term

from .assessment import (
    ACTIVE_STATUSES,
    LEFT_STATUSES,
    AssessmentError,
    active_target,
    current_term,
    expel,
    promote,
    promotion_items,
    record_scores,
    term_interval,
    withdraw,
)
from .auth import (
    active_member,
    allows,
    audit,
    authenticated,
    client_ip,
    csrf_form,
    csrf_matches,
    current_member,
    deny,
    permission_codes,
    require_permission,
    unauthorized,
    validate_csrf,
)
from .db import SessionLocal, get_session
from .markdown import render
from .models import (
    AssessmentTest,
    Attachment,
    Checkin,
    CheckinProblem,
    Department,
    JobRun,
    KnowledgePoint,
    Member,
    MemberApplication,
    MemberAssessment,
    MemberAward,
    Problem,
    ProblemKp,
    ProblemSolution,
    SysConfig,
    TaskAssignee,
    TaskProblem,
    TestScore,
    TrainingPhase,
    TrainingPlan,
    TrainingTask,
)
from .models.base import local_time
from .profile import (
    assessment_history,
    award_fields,
    award_summary,
    awards_of,
    can_view_sensitive,
    csv_filename_for,
    format_points,
    full_csv_sections,
    mask_contact,
    member_stats,
    profile_csv_sections,
    render_csv_sections,
    training_summary,
)
from .security import (
    create_session,
    hash_password,
    load_session,
    revoke_all_sessions,
    revoke_session,
    rotate_session,
    token_digest,
    valid_password,
    verify_password,
)
from .storage import CHUNK_BYTES, MAX_UPLOAD_BYTES, resolve_upload_path, stage_photo
from .taxonomy import (
    TaxonomyError,
    can_delete,
    move_subtree,
    next_depth_path,
    normalise_name,
    validate_parent,
)
from .training import (
    MEMBER_ASSIGNEE,
    TASK_ASSIGNEE_TYPES,
    active_members_query,
    alert_items,
    alert_query,
    assignee_ids,
    checkin_details,
    makeup_checkin,
    member_summaries,
    member_tasks_query,
    named_assignees,
    publish_task,
    task_progress,
    task_totals,
    visible_to_member,
)
from .web import content_type, page, parse_object_id, success, wants_html

router = APIRouter()

STUDENT_NO_RE = re.compile(r"^[0-9]{8,20}$")
PHONE_RE = re.compile(r"^[0-9]{11}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
APPLICATION_STATUSES = ("待审核", "待面试", "已面试", "已通过", "已拒绝", "已撤回")
APPLICATION_APPROVER = require_permission("member:approve", target_type="application")
APPLICATION_READER = require_permission("member:approve", "member:read", target_type="application")

PROBLEM_TYPES = ("单选", "多选", "程序", "填空")
PROBLEM_DIFFICULTIES = ("入门", "提高", "进阶", "区域赛金", "区域赛银", "区域赛铜")
KP_DIFFICULTIES = ("入门", "提高", "进阶", "区域赛")
PROBLEM_READER = require_permission("problem:read", target_type="problem")
PROBLEM_WRITER = require_permission("problem:write", target_type="problem")

TRAINING_READER = require_permission("training:read", target_type="training")
TRAINING_WRITER = require_permission("training:write", target_type="training")
TRAINING_APPROVER = require_permission("training:approve", target_type="training")
CHECKIN_WRITER = require_permission("checkin:write:self", target_type="training")
PLAN_INTENSITIES = ("低", "中", "高")
SHANGHAI = ZoneInfo("Asia/Shanghai")


def set_session_cookie(request, response, token, ttl):
    response.set_cookie(
        "sid", token, max_age=ttl * 3600, path="/", httponly=True,
        secure=request.app.state.settings.cookie_secure, samesite="lax",
    )
    return response


def redirect(path):
    return RedirectResponse(path, status_code=303)


def invalid(message="请检查填写的内容"):
    return HTTPException(400, message)


def object_id(id: str = Path()):
    try:
        return parse_object_id(id)
    except ValueError:
        raise invalid("参数校验失败") from None


async def payload(request: Request):
    if content_type(request) != "application/json":
        return await csrf_form(request)
    try:
        data = await request.json()
    except ValueError:
        raise invalid() from None
    if not isinstance(data, dict):
        raise invalid()
    return data


def field(data, name, limit, *, required=False):
    value = data.get(name, "")
    if not isinstance(value, str):
        raise invalid()
    value = value.strip()
    if len(value) > limit or (required and not value):
        raise invalid()
    return value or None


def application_data(application):
    return {
        "id": application.id, "student_no": application.student_no, "name": application.name,
        "major": application.major, "grade": application.grade, "phone": application.phone,
        "email": application.email, "apply_type": application.apply_type, "status": application.status,
        "review_note": application.review_note, "interview_note": application.interview_note,
        "interviewed_at": application.interviewed_at, "reviewed_at": application.reviewed_at,
        "member_id": application.member_id, "created_at": application.created_at,
    }


@router.get("/login")
def login_page(request: Request):
    return page(request, "login.html")


@router.post("/auth/login")
def login(request: Request, data=Depends(payload), session: Session = Depends(get_session)):
    student_no = data.get("student_no")
    password = data.get("password")
    member = None
    if isinstance(student_no, str) and 0 < len(student_no.strip()) <= 20:
        member = session.scalar(select(Member).where(Member.student_no == student_no.strip()))
    member_id = member.id if member else None
    previous_hash = member.password_hash if active_member(member) else None
    valid = previous_hash and isinstance(password, str) and verify_password(password, previous_hash)
    if valid:
        # Recheck the credential after acquiring the write lock to serialize with password changes.
        session.rollback()
        session.execute(text("BEGIN IMMEDIATE"))
        member = session.get(Member, member_id)
        valid = active_member(member) and member.password_hash == previous_hash
    if not valid:
        message = "学号或密码错误"
        audit(request, session, "auth.login.failed", target_id=member_id)
        session.commit()
        if wants_html(request):
            return page(request, "login.html", status=401, error_code=40101, error_message=message)
        raise HTTPException(401, message)

    ttl = request.app.state.settings.session_ttl_hours
    previous_sid = request.cookies.get("sid")
    if previous_sid:
        revoke_session(previous_sid, session=session, reason="login")
    token = create_session(
        member.id, request.headers.get("user-agent"), client_ip(request),
        session=session, ttl_hours=ttl,
    )
    audit(request, session, "auth.login.success", actor_id=member.id, target_id=member.id)
    session.commit()
    request.state.member_id = member.id
    return set_session_cookie(
        request, redirect("/me/password" if member.must_change_password else "/"), token, ttl,
    )


@router.post("/auth/logout")
def logout(request: Request, member: Member = Depends(authenticated),
           session: Session = Depends(get_session)):
    revoke_session(request.cookies["sid"], session=session)
    audit(request, session, "auth.logout", actor_id=member.id, target_id=member.id)
    session.commit()
    response = redirect("/login") if wants_html(request) else Response(status_code=204)
    response.delete_cookie(
        "sid", path="/", httponly=True, secure=request.app.state.settings.cookie_secure, samesite="lax",
    )
    return response


@router.get("/")
def home(_member: Member = Depends(authenticated)):
    return redirect("/members")


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

    filename = f"members-{datetime.now(SHANGHAI).strftime('%Y%m%d')}.csv"
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


@router.get("/me")
def me(request: Request, member: Member = Depends(authenticated),
       session: Session = Depends(get_session)):
    department = session.scalar(select(Department.name).where(Department.id == member.dept_id))
    return success(member_data(member, department) | {
        "role": member.role, "permissions": sorted(permission_codes(member, session)),
        "csrf_token": request.state.csrf_token,
    })


@router.get("/me/password")
def password_page(request: Request, _member: Member = Depends(authenticated)):
    return page(request, "password.html")


@router.put("/me/password")
@router.post("/me/password")
def change_password(request: Request, member: Member = Depends(authenticated),
                    data=Depends(payload), session: Session = Depends(get_session)):
    old_password = data.get("old_password")
    new_password = data.get("new_password")
    if not isinstance(new_password, str) or not valid_password(new_password):
        raise invalid("新密码须为 1–72 个 UTF-8 字节，且不能含空字符")
    previous_hash = member.password_hash
    if not isinstance(old_password, str) or not verify_password(old_password, previous_hash):
        raise HTTPException(400, {"code": 40002, "message": "旧密码错误"})
    replacement_hash = hash_password(new_password)

    member_id = member.id
    sid = request.cookies["sid"]
    session.rollback()
    session.execute(text("BEGIN IMMEDIATE"))
    member = session.get(Member, member_id)
    if not active_member(member) or not load_session(sid, session=session):
        raise unauthorized()
    if member.password_hash != previous_hash:
        raise HTTPException(409, {"code": 40902, "message": "密码已变更，请重试"})
    if member.must_change_password and old_password == new_password:
        raise invalid("新密码不能与一次性密码相同")
    member.password_hash = replacement_hash
    member.must_change_password = 0
    member.updated_at = local_time()
    ttl = request.app.state.settings.session_ttl_hours
    revoke_all_sessions(member.id, sid, "password_change", session=session)
    token = rotate_session(sid, session=session, ttl_hours=ttl, reason="password_change")
    if token is None:
        raise unauthorized()
    audit(request, session, "auth.password.changed", actor_id=member.id, target_id=member.id)
    session.commit()

    response = redirect("/members") if wants_html(request) else Response(status_code=204)
    return set_session_cookie(request, response, token, ttl)


def apply_form_response(request, *, status=200, code=None, message=None, values=None):
    token = secrets.token_urlsafe(32)
    response = page(
        request, "apply.html", status=status, error_code=code, error_message=message,
        csrf_token=token, values=values or {},
    )
    response.set_cookie(
        "csrf_anon", token, max_age=1800, path="/", httponly=True,
        secure=request.app.state.settings.cookie_secure, samesite="lax",
    )
    return response


def parse_apply_form(form):
    values = {}
    errors = []

    student_no = form.get("student_no", "")
    student_no = student_no.strip() if isinstance(student_no, str) else ""
    values["student_no"] = student_no
    if not STUDENT_NO_RE.fullmatch(student_no):
        errors.append("学号须为 8–20 位数字")

    name = form.get("name", "")
    name = name.strip() if isinstance(name, str) else ""
    values["name"] = name
    if not 2 <= len(name) <= 20:
        errors.append("姓名须为 2–20 个字")

    for key, label, limit in (("major", "专业", 80), ("phone", "手机", 20), ("email", "邮箱", 120)):
        value = form.get(key, "")
        if not isinstance(value, str):
            errors.append(f"{label}格式不正确")
            value = ""
        value = value.strip()
        values[key] = value
        if len(value) > limit:
            errors.append(f"{label}不能超过 {limit} 个字符")

    grade = form.get("grade", "")
    if not isinstance(grade, str):
        errors.append("入学年份须为 2000–2100 的数字")
        grade = ""
    grade = grade.strip()
    values["grade"] = grade
    if grade:
        if not re.fullmatch(r"[0-9]{4}", grade):
            errors.append("入学年份须为 2000–2100 的数字")
        elif not 2000 <= int(grade) <= 2100:
            errors.append("入学年份须在 2000–2100 之间")

    phone = values["phone"]
    if phone and not PHONE_RE.fullmatch(phone):
        errors.append("手机号须为 11 位数字")

    email = values["email"]
    if email and not EMAIL_RE.fullmatch(email):
        errors.append("邮箱格式不正确")

    if form.get("agree_observation", "") != "1":
        errors.append("请勾选「已知悉观察期规则」")

    return values, errors


@router.get("/apply")
def apply_page(request: Request):
    return apply_form_response(request)


@router.post("/apply")
def submit_application(request: Request, form=Depends(csrf_form),
                       session: Session = Depends(get_session)):
    tokens = form.getlist("csrf_token")
    if len(tokens) != 1 or not csrf_matches(request.cookies.get("csrf_anon"), tokens[0]):
        if wants_html(request):
            return apply_form_response(
                request, status=400, code=40002, message="安全令牌无效，请重新提交", values=form,
            )
        return JSONResponse(
            {"code": 40002, "message": "安全令牌无效，请重新打开申请页", "data": None}, status_code=400,
        )

    values, errors = parse_apply_form(form)
    if errors:
        message = "；".join(errors)
        if wants_html(request):
            return apply_form_response(request, status=400, code=40001, message=message, values=form)
        return JSONResponse({"code": 40001, "message": message, "data": None}, status_code=400)

    receipt_code = secrets.token_urlsafe(16)
    statement = insert(MemberApplication).values(
        student_no=values["student_no"], name=values["name"], major=values["major"],
        grade=int(values["grade"]) if values["grade"] else None,
        phone=values["phone"], email=values["email"],
        apply_type="入会", status="待审核", active_flag="1", created_at=local_time(),
        receipt_code_hash=token_digest(receipt_code),
    ).on_conflict_do_nothing(index_elements=["student_no", "apply_type", "active_flag"])
    if session.execute(statement).rowcount == 0:
        existing_id = session.scalar(select(MemberApplication.id).where(
            MemberApplication.student_no == values["student_no"],
            MemberApplication.apply_type == "入会", MemberApplication.active_flag == "1",
        ))
        message = "该学号已有待处理的入会申请"
        if wants_html(request):
            return apply_form_response(request, status=409, code=40901, message=message, values=form)
        return JSONResponse(
            {"code": 40901, "message": message, "data": {"id": existing_id}}, status_code=409,
        )

    application = session.scalar(select(MemberApplication).where(
        MemberApplication.student_no == values["student_no"],
        MemberApplication.apply_type == "入会", MemberApplication.active_flag == "1",
    ))
    session.commit()
    return page(request, "apply_success.html", application_no=application.id, receipt_code=receipt_code)


@router.get("/apply/status")
@router.post("/apply/status")
async def apply_status(request: Request, session: Session = Depends(get_session)):
    data = await payload(request) if request.method == "POST" else request.query_params
    receipt_code = data.get("receipt_code")
    queried = request.method == "POST" or bool(request.query_params)
    result = None
    if isinstance(receipt_code, str) and re.fullmatch(r"[A-Za-z0-9_-]{22}", receipt_code):
        row = session.scalar(select(MemberApplication).where(
            MemberApplication.receipt_code_hash == token_digest(receipt_code),
        ))
        if row is not None:
            result = {"id": row.id, "status": row.status, "apply_type": row.apply_type,
                      "created_at": row.created_at}
    return page(request, "apply_status.html", status=404 if queried and result is None else 200,
                result=result, queried=queried, headers={"Referrer-Policy": "no-referrer"})


def parse_application_result(data, allowed):
    result = data.get("result")
    if not isinstance(result, str) or result.strip() not in allowed:
        raise HTTPException(400, {"code": 40001, "message": "请选择有效的处理结果"})
    return result.strip()


def parse_application_note(data, name, label):
    note = data.get(name, "")
    note = note.strip() if isinstance(note, str) else ""
    if not note:
        raise HTTPException(400, {"code": 40001, "message": f"请填写{label}"})
    if len(note) > 500:
        raise HTTPException(400, {"code": 40001, "message": f"{label}不能超过 500 字"})
    return note


@router.get("/admin/applications")
def admin_applications(request: Request, status: str | None = Query(None),
                       page_number: int = Query(1, alias="page", ge=1, le=2**63 // 100),
                       size: int = Query(20, ge=1, le=100),
                       member: Member = Depends(APPLICATION_READER),
                       session: Session = Depends(get_session)):
    filters = [MemberApplication.apply_type == "入会"]
    if status:
        filters.append(MemberApplication.status == status)
    total = session.scalar(select(func.count()).select_from(MemberApplication).where(*filters))
    rows = session.scalars(
        select(MemberApplication).where(*filters)
        .order_by(MemberApplication.created_at.desc(), MemberApplication.id.desc())
        .offset((page_number - 1) * size).limit(size)
    ).all()
    data = {
        "total": total, "page": page_number, "size": size,
        "items": [application_data(item) for item in rows],
    }
    if wants_html(request):
        departments = session.scalars(select(Department).order_by(Department.id)).all()
        return page(
            request, "applications.html", pagination=data, departments=departments,
            current_status=status or "", statuses=APPLICATION_STATUSES,
            can_approve=allows(permission_codes(member, session), "member:approve"),
        )
    return success(data)


def transition_application(session, application_id, expected_status, operation, **values):
    result = session.execute(update(MemberApplication).where(
        MemberApplication.id == application_id, MemberApplication.status == expected_status,
        MemberApplication.active_flag == "1", MemberApplication.apply_type == "入会",
    ).values(**values))
    if result.rowcount == 0:
        raise HTTPException(400, {"code": 40002, "message": f"该申请当前状态不可{operation}"})


@router.post("/admin/applications/{id}/review")
@router.post("/member-applications/{id}/review")
def review_application(request: Request, id: int = Depends(object_id),
                       data=Depends(payload),
                       member: Member = Depends(APPLICATION_APPROVER),
                       session: Session = Depends(get_session)):
    result = parse_application_result(data, {"通过", "拒绝"})
    note = parse_application_note(data, "review_note", "审核意见")
    passed = result == "通过"
    status = "待面试" if passed else "已拒绝"
    transition_application(
        session, id, "待审核", "审核", status=status, active_flag="1" if passed else None,
        review_note=note, reviewed_by=member.id, reviewed_at=local_time(),
    )
    audit(request, session, "application.review", actor_id=member.id, target_id=id,
          target_type="application", detail={"result": result, "status": status})
    session.commit()
    if wants_html(request):
        return redirect("/admin/applications")
    return success({"id": id, "status": status})


@router.post("/admin/applications/{id}/interview")
@router.post("/member-applications/{id}/interview")
def interview_application(request: Request, id: int = Depends(object_id),
                          data=Depends(payload),
                          member: Member = Depends(APPLICATION_APPROVER),
                          session: Session = Depends(get_session)):
    result = parse_application_result(data, {"通过", "不通过"})
    note = parse_application_note(data, "interview_note", "面试意见")
    passed = result == "通过"
    status = "已面试" if passed else "已拒绝"
    transition_application(
        session, id, "待面试", "面试", status=status, active_flag="1" if passed else None,
        interview_note=note, interviewed_at=local_time(),
    )
    audit(request, session, "application.interview", actor_id=member.id, target_id=id,
          target_type="application", detail={"result": result, "status": status})
    session.commit()
    if wants_html(request):
        return redirect("/admin/applications")
    return success({"id": id, "status": status})


def ensure_member_for_application(session, application, now, dept_id, password_hash):
    existing = session.scalar(select(Member).where(Member.student_no == application.student_no))
    if existing is not None:
        message = "会员已存在" if active_member(existing) else "需先由管理员恢复会员"
        raise HTTPException(400, {"code": 40002, "message": message})
    member = Member(
        student_no=application.student_no, name=application.name,
        major=application.major, grade=application.grade, phone=application.phone,
        email=application.email, password_hash=password_hash,
        member_type="观察会员", role="member", member_status="观察会员",
        dept_id=dept_id, joined_on=now[:10], must_change_password=1,
        created_at=now, updated_at=now,
    )
    session.add(member)
    session.flush()
    return member


@router.post("/admin/applications/{id}/admit")
@router.post("/member-applications/{id}/admit")
def admit_application(request: Request, id: int = Depends(object_id),
                      data=Depends(payload),
                      member: Member = Depends(APPLICATION_APPROVER),
                      session: Session = Depends(get_session)):
    dept_id = None
    raw_dept = data.get("dept_id")
    if raw_dept not in (None, ""):
        try:
            dept_id = parse_object_id(str(raw_dept).strip())
        except ValueError:
            raise invalid("部门选择无效") from None
        if session.get(Department, dept_id) is None:
            raise invalid("部门不存在")

    password = secrets.token_urlsafe(12)
    password_hash = hash_password(password)
    transition_application(session, id, "已面试", "录取", status="已通过", active_flag=None)
    application = session.get(MemberApplication, id)
    admitted = ensure_member_for_application(session, application, local_time(), dept_id, password_hash)
    application.member_id = admitted.id
    audit(request, session, "application.admit", actor_id=member.id, target_id=id,
          target_type="application", detail={"member_id": admitted.id, "must_change_password": 1,
                                             "one_time_password_issued": True})
    session.commit()
    return page(request, "admit_success.html", admitted=admitted, one_time_password=password)


def api_error(status, code, message, data=None):
    return HTTPException(status, {"code": code, "message": message, "data": data})


def required_text(data, name, label, limit):
    value = data.get(name)
    if not isinstance(value, str):
        raise api_error(400, 40001, f"请填写{label}")
    value = value.strip()
    if not value or len(value) > limit:
        raise api_error(400, 40001, f"{label}须为 1–{limit} 个字符")
    return value


def optional_text(data, name, limit):
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise api_error(400, 40001, "参数格式不正确")
    value = value.strip()
    if len(value) > limit:
        raise api_error(400, 40001, f"{name}不能超过 {limit} 个字符")
    return value or None


def markdown_text(data, name, limit):
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise api_error(400, 40001, "参数格式不正确")
    if len(value) > limit:
        raise api_error(400, 40001, f"{name}不能超过 {limit} 个字符")
    return value or None


def enum_field(data, name, allowed, label, *, default=None):
    value = data.get(name)
    if value in (None, ""):
        if default is not None:
            return default
        raise api_error(400, 40001, f"请选择{label}")
    if not isinstance(value, str) or value.strip() not in allowed:
        raise api_error(400, 40001, f"{label}无效")
    return value.strip()


def int_field(data, name, *, required=False):
    raw = data.get(name)
    if raw in (None, ""):
        if required:
            raise api_error(400, 40001, "参数校验失败")
        return None
    try:
        return parse_object_id(str(raw).strip())
    except ValueError:
        raise api_error(400, 40001, "参数校验失败") from None


def kp_ids_field(data):
    if isinstance(data, FormData):
        raw = data.getlist("kp_ids") or data.get("kp_id")
    else:
        raw = data.get("kp_ids", data.get("kp_id"))
    if raw in (None, ""):
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise api_error(400, 40001, "知识点参数无效")
    parts = [
        part.strip()
        for item in raw
        for part in str(item).replace("，", ",").split(",")
        if part.strip()
    ]
    ids = []
    for part in parts:
        try:
            ids.append(parse_object_id(part))
        except ValueError:
            raise api_error(400, 40001, "知识点参数无效") from None
    return ids


def knowledge_point_count_map(session):
    rows = session.execute(
        select(ProblemKp.kp_id, func.count())
        .select_from(ProblemKp)
        .join(Problem, Problem.id == ProblemKp.problem_id)
        .where(Problem.deleted_at.is_(None))
        .group_by(ProblemKp.kp_id)
    ).all()
    return dict(rows)


def knowledge_point_item(kp, count_map):
    return {
        "id": kp.id, "name": kp.name, "parent_id": kp.parent_id,
        "depth": kp.depth, "path": kp.path, "difficulty": kp.difficulty,
        "problem_count": count_map.get(kp.id, 0),
    }


def problem_kps(session, problem_ids):
    grouped = {problem_id: [] for problem_id in problem_ids}
    if grouped:
        rows = session.execute(
            select(ProblemKp.problem_id, KnowledgePoint)
            .join(KnowledgePoint, KnowledgePoint.id == ProblemKp.kp_id)
            .where(ProblemKp.problem_id.in_(grouped))
            .order_by(KnowledgePoint.path)
        )
        for problem_id, kp in rows:
            grouped[problem_id].append({"id": kp.id, "name": kp.name, "path": kp.path})
    return grouped


def problem_solutions_payload(solutions, session):
    payload = []
    for solution in solutions:
        author = session.get(Member, solution.author_id) if solution.author_id else None
        payload.append({
            "id": solution.id, "author_id": solution.author_id,
            "author_name": author.name if author else None,
            "created_at": solution.created_at, "content_md": solution.content_md,
            "content_html": render(solution.content_md),
        })
    return payload


def problem_solutions(problem_id, session):
    solutions = session.scalars(
        select(ProblemSolution).where(ProblemSolution.problem_id == problem_id)
        .order_by(ProblemSolution.id)
    ).all()
    return problem_solutions_payload(solutions, session)


def problem_payload(problem, session):
    return {
        "id": problem.id, "title": problem.title, "source": problem.source,
        "platform": problem.platform, "external_id": problem.external_id,
        "difficulty": problem.difficulty, "problem_type": problem.problem_type,
        "statement": problem.statement, "statement_html": render(problem.statement or ""),
        "sample_in": problem.sample_in, "sample_in_html": render(problem.sample_in or ""),
        "sample_out": problem.sample_out, "sample_out_html": render(problem.sample_out or ""),
        "created_at": problem.created_at, "updated_at": problem.updated_at,
        "kps": problem_kps(session, [problem.id])[problem.id],
        "solutions": problem_solutions(problem.id, session),
    }


def problem_item(problem, kps, solution_count):
    return {
        "id": problem.id, "title": problem.title, "source": problem.source,
        "difficulty": problem.difficulty, "problem_type": problem.problem_type,
        "platform": problem.platform, "external_id": problem.external_id,
        "created_at": problem.created_at,
        "kp_names": [kp["name"] for kp in kps],
        "solution_count": solution_count,
    }


@contextmanager
def bank_write(session, conflict_message, *, legacy_busy_response=True):
    try:
        session.execute(text("BEGIN IMMEDIATE"))
        yield
        session.commit()
    except OperationalError as exc:
        session.rollback()
        sqlite_code = getattr(exc.orig, "sqlite_errorcode", 0) & 0xff
        if legacy_busy_response and sqlite_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            raise api_error(409, 40901, "题库正在写入，请稍后重试") from None
        raise
    except IntegrityError as exc:
        session.rollback()
        if getattr(exc.orig, "sqlite_errorname", None) == "SQLITE_CONSTRAINT_FOREIGNKEY":
            raise api_error(400, 40002, "关联记录不存在或仍被引用") from None
        raise api_error(409, 40901, conflict_message) from None


def validated_render(value):
    try:
        return render(value or "")
    except (ValueError, RecursionError):
        raise api_error(400, 40001, "Markdown 内容无法渲染") from None


def apply_problem(data, session, problem, *, member):
    title = required_text(data, "title", "标题", 200)
    difficulty = enum_field(data, "difficulty", PROBLEM_DIFFICULTIES, "难度")
    problem_type = enum_field(data, "problem_type", PROBLEM_TYPES, "题型")
    source = optional_text(data, "source", 100)
    platform = optional_text(data, "platform", 40)
    external_id = optional_text(data, "external_id", 100)
    statement = markdown_text(data, "statement", 20000)
    samples = {
        name: markdown_text(data, name, 20000) if problem is None or name in data
        else getattr(problem, name)
        for name in ("sample_in", "sample_out")
    }
    sample_in, sample_out = samples.values()
    if problem is None or samples.keys() & data.keys():
        if not all(sample and sample.strip() for sample in samples.values()):
            raise api_error(400, 40001, "请填写样例输入和样例输出")
    for value in (statement, sample_in, sample_out):
        validated_render(value)

    if platform and external_id:
        statement_row = select(Problem).where(
            Problem.platform == platform, Problem.external_id == external_id,
        )
        if problem is not None:
            statement_row = statement_row.where(Problem.id != problem.id)
        existing = session.scalar(statement_row)
        if existing is not None:
            raise api_error(409, 40901, "该平台的外部题目已存在", {"id": existing.id})

    if problem is None:
        problem = Problem(
            title=title, source=source, platform=platform, external_id=external_id,
            difficulty=difficulty, problem_type=problem_type, statement=statement,
            sample_in=sample_in, sample_out=sample_out,
            created_by=member.id, created_at=local_time(), updated_at=local_time(),
        )
        session.add(problem)
        session.flush()
    else:
        problem.title = title
        problem.source = source
        problem.platform = platform
        problem.external_id = external_id
        problem.difficulty = difficulty
        problem.problem_type = problem_type
        problem.statement = statement
        problem.sample_in = sample_in
        problem.sample_out = sample_out
        problem.updated_at = local_time()
    return problem


def link_problem_kps(session, problem_id, kp_ids):
    for kp_id in dict.fromkeys(kp_ids):
        if session.get(KnowledgePoint, kp_id) is None:
            raise api_error(400, 40002, "关联的知识点不存在")
    session.execute(delete(ProblemKp).where(ProblemKp.problem_id == problem_id))
    for kp_id in dict.fromkeys(kp_ids):
        session.add(ProblemKp(problem_id=problem_id, kp_id=kp_id))


@router.get("/knowledge-points")
def knowledge_points(request: Request, member: Member = Depends(PROBLEM_READER),
                     session: Session = Depends(get_session)):
    points = session.scalars(
        select(KnowledgePoint).order_by(KnowledgePoint.path, KnowledgePoint.id)
    ).all()
    count_map = knowledge_point_count_map(session)
    data = {
        "total": len(points),
        "items": [knowledge_point_item(kp, count_map) for kp in points],
    }
    if wants_html(request):
        return page(request, "knowledge_points.html", tree=data)
    return success(data)


@router.post("/knowledge-points")
def create_knowledge_point(request: Request, data=Depends(payload),
                           member: Member = Depends(PROBLEM_WRITER),
                           session: Session = Depends(get_session)):
    with bank_write(session, "同级知识点已存在同名项"):
        raw_name = data.get("name")
        if not isinstance(raw_name, str):
            raise api_error(400, 40001, "请填写知识点名称")
        try:
            name = normalise_name(raw_name)
        except ValueError as exc:
            raise api_error(400, 40001, str(exc)) from None

        parent_id = int_field(data, "parent_id")
        try:
            validate_parent(session, None, parent_id)
        except TaxonomyError as exc:
            raise api_error(400, exc.code, str(exc)) from None

        duplicate = session.scalar(select(KnowledgePoint.id).where(
            KnowledgePoint.parent_id == parent_id, KnowledgePoint.name == name,
        ))
        if duplicate is not None:
            raise api_error(409, 40901, "同级知识点已存在同名项", {"id": duplicate})

        difficulty = enum_field(data, "difficulty", KP_DIFFICULTIES, "难度", default="入门")
        parent = session.get(KnowledgePoint, parent_id) if parent_id is not None else None
        depth, prefix = next_depth_path(parent)
        node = KnowledgePoint(
            parent_id=parent_id, name=name, difficulty=difficulty, depth=depth, path=prefix,
            created_at=local_time(),
        )
        session.add(node)
        session.flush()
        node.path = prefix + str(node.id) + "/"
        audit(request, session, "knowledge_point.created", actor_id=member.id, target_id=node.id,
              target_type="knowledge_point", detail={"name": name, "parent_id": parent_id})
    if wants_html(request):
        return redirect("/knowledge-points")
    return success({"id": node.id, "name": node.name, "parent_id": node.parent_id,
                    "depth": node.depth, "path": node.path})


@router.put("/knowledge-points/{id}")
def edit_knowledge_point(request: Request, id: int = Depends(object_id), data=Depends(payload),
                         member: Member = Depends(PROBLEM_WRITER),
                         session: Session = Depends(get_session)):
    with bank_write(session, "同级知识点已存在同名项"):
        node = session.get(KnowledgePoint, id)
        if node is None:
            raise api_error(404, 40401, "知识点不存在")
        if "name" in data:
            try:
                node.name = normalise_name(data["name"])
            except ValueError as exc:
                raise api_error(400, 40001, str(exc)) from None
        if "difficulty" in data:
            node.difficulty = enum_field(data, "difficulty", KP_DIFFICULTIES, "难度")
        audit(request, session, "knowledge_point.updated", actor_id=member.id, target_id=id,
              target_type="knowledge_point", detail={"name": node.name, "difficulty": node.difficulty})
    return success(knowledge_point_item(node, knowledge_point_count_map(session)))


@router.post("/knowledge-points/{id}/move")
def move_knowledge_point(request: Request, id: int = Depends(object_id), data=Depends(payload),
                         member: Member = Depends(PROBLEM_WRITER),
                         session: Session = Depends(get_session)):
    with bank_write(session, "目标位置已存在同名知识点"):
        node = session.get(KnowledgePoint, id)
        if node is None:
            raise api_error(404, 40401, "知识点不存在")
        new_parent_id = int_field(data, "parent_id")
        duplicate = session.scalar(select(KnowledgePoint.id).where(
            KnowledgePoint.parent_id == new_parent_id, KnowledgePoint.name == node.name,
            KnowledgePoint.id != id,
        ))
        if duplicate is not None:
            raise api_error(409, 40901, "目标位置已存在同名知识点", {"id": duplicate})
        try:
            affected = move_subtree(session, id, new_parent_id)
        except TaxonomyError as exc:
            raise api_error(400, exc.code, str(exc)) from None
        audit(request, session, "knowledge_point.moved", actor_id=member.id, target_id=id,
              target_type="knowledge_point", detail={"new_parent_id": new_parent_id, "affected": affected})
    if wants_html(request):
        return redirect("/knowledge-points")
    return success({"id": id, "affected": affected, "depth": node.depth, "path": node.path})


@router.delete("/knowledge-points/{id}")
@router.post("/knowledge-points/{id}/delete")
def delete_knowledge_point(request: Request, id: int = Depends(object_id),
                           member: Member = Depends(PROBLEM_WRITER),
                           session: Session = Depends(get_session)):
    with bank_write(session, "知识点删除冲突"):
        node = session.get(KnowledgePoint, id)
        if node is None:
            raise api_error(404, 40401, "知识点不存在")
        child_count = can_delete(session, id)
        if child_count:
            raise api_error(400, 40002, "该知识点下还有子知识点，不能删除",
                            {"child_count": child_count})
        linked_problems = session.scalar(
            select(func.count()).select_from(ProblemKp).where(ProblemKp.kp_id == id)
        )
        if linked_problems:
            raise api_error(400, 40002, "该知识点下有关联题目，不能删除",
                            {"problem_count": linked_problems})
        session.delete(node)
        audit(request, session, "knowledge_point.deleted", actor_id=member.id, target_id=id,
              target_type="knowledge_point", detail={"name": node.name})
    if wants_html(request):
        return redirect("/knowledge-points")
    return success({"deleted": True, "id": id})


@router.get("/problems/stats")
def problem_stats(request: Request, member: Member = Depends(PROBLEM_READER),
                  session: Session = Depends(get_session)):
    kp_rows = session.execute(
        select(KnowledgePoint.id, KnowledgePoint.name, KnowledgePoint.path, func.count(Problem.id))
        .outerjoin(ProblemKp, ProblemKp.kp_id == KnowledgePoint.id)
        .outerjoin(Problem, (Problem.id == ProblemKp.problem_id) & Problem.deleted_at.is_(None))
        .group_by(KnowledgePoint.id, KnowledgePoint.name, KnowledgePoint.path)
        .order_by(KnowledgePoint.path)
    ).all()
    difficulty_rows = session.execute(
        select(Problem.difficulty, func.count())
        .where(Problem.deleted_at.is_(None))
        .group_by(Problem.difficulty)
    ).all()
    type_rows = session.execute(
        select(Problem.problem_type, func.count())
        .where(Problem.deleted_at.is_(None))
        .group_by(Problem.problem_type)
    ).all()
    tables = inspect(session.connection())
    task_refs = {}
    if tables.has_table("task_problem"):
        task_refs = dict(session.execute(
            text("SELECT problem_id, count(*) FROM task_problem GROUP BY problem_id")
        ).all())
    correct_rates = {}
    if tables.has_table("oj_submission"):
        correct_rates = dict(session.execute(text(
            "SELECT problem_id, avg(CASE WHEN verdict = 'AC' THEN 1.0 ELSE 0.0 END) "
            "FROM oj_submission WHERE problem_id IS NOT NULL GROUP BY problem_id"
        )).all())
    problems = session.scalars(
        select(Problem).where(Problem.deleted_at.is_(None)).order_by(Problem.id)
    ).all()
    data = {
        "kp_counts": [
            {"kp_id": kp_id, "kp_name": name, "kp_path": path, "problem_count": count}
            for kp_id, name, path, count in kp_rows
        ],
        "difficulty_counts": dict(difficulty_rows),
        "type_counts": dict(type_rows),
        "problem_references": [
            {"problem_id": problem.id, "title": problem.title,
             "referenced_count": task_refs.get(problem.id, 0),
             "correct_rate": correct_rates.get(problem.id)}
            for problem in problems
        ],
    }
    if wants_html(request):
        return page(request, "problem_stats.html", stats=data)
    return success(data)


def search_value(value):
    value = (value or "").strip()
    return None if value in ("", "全部") else value


@router.get("/problems")
def problem_list(request: Request, kp: str | None = Query(None),
                 difficulty: str | None = Query(None), type: str | None = Query(None),
                 source: str | None = Query(None), keyword: str | None = Query(None),
                 page_number: int = Query(1, alias="page", ge=1, le=2**63 // 100),
                 size: int = Query(20, ge=1, le=100),
                 member: Member = Depends(PROBLEM_READER),
                 session: Session = Depends(get_session)):
    """筛选值去首尾空白。来源区分大小写且精确匹配，关键词按字面查找，忽略 ASCII 大小写。"""
    kp = search_value(kp if kp is not None else request.query_params.get("kpId"))
    type = search_value(type if type is not None else request.query_params.get("problemType"))
    difficulty, source, keyword = map(search_value, (difficulty, source, keyword))
    filters = [Problem.deleted_at.is_(None)]
    if kp is not None:
        kp = int_field({"kp": kp}, "kp")
        node = session.get(KnowledgePoint, kp)
        if node is None:
            filters.append(false())
        else:
            filters.append(Problem.id.in_(
                select(ProblemKp.problem_id).where(ProblemKp.kp_id.in_(
                    select(KnowledgePoint.id).where(KnowledgePoint.path.like(node.path + "%"))
                ))
            ))
    if difficulty:
        filters.append(Problem.difficulty == difficulty)
    if type:
        filters.append(Problem.problem_type == type)
    if source:
        filters.append(Problem.source == source)
    if keyword:
        filters.append(or_(
            Problem.title.contains(keyword, autoescape=True),
            Problem.statement.contains(keyword, autoescape=True),
        ))

    total = session.scalar(select(func.count()).select_from(Problem).where(*filters))
    rows = session.scalars(
        select(Problem).where(*filters).order_by(Problem.id.desc())
        .offset((page_number - 1) * size).limit(size)
    ).all()
    kps = problem_kps(session, [problem.id for problem in rows])
    solution_counts = dict(session.execute(
        select(ProblemSolution.problem_id, func.count())
        .where(ProblemSolution.problem_id.in_(kps))
        .group_by(ProblemSolution.problem_id)
    ).all()) if kps else {}
    data = {
        "total": total, "page": page_number, "size": size,
        "items": [problem_item(problem, kps[problem.id], solution_counts.get(problem.id, 0))
                  for problem in rows],
    }
    if wants_html(request):
        values = {"kp": kp, "difficulty": difficulty, "type": type,
                  "source": source, "keyword": keyword}
        page_query = urlencode({"size": size, **{k: v for k, v in values.items() if v is not None}})
        return page(
            request, "problems.html", pagination=data, filters=values, page_query=page_query,
            knowledge_points=session.scalars(
                select(KnowledgePoint).order_by(KnowledgePoint.path)
            ).all(),
            difficulties=PROBLEM_DIFFICULTIES, types=PROBLEM_TYPES,
        )
    return success(data)


def active_problem(session, problem_id):
    problem = session.get(Problem, problem_id)
    if problem is None or problem.deleted_at is not None:
        raise api_error(404, 40401, "题目不存在")
    return problem


@router.get("/problems/{id}")
def problem_detail(request: Request, id: int = Depends(object_id),
                   member: Member = Depends(PROBLEM_READER),
                   session: Session = Depends(get_session)):
    problem = active_problem(session, id)
    data = problem_payload(problem, session)
    if wants_html(request):
        return page(request, "problem.html", problem=data)
    return success(data)


@router.post("/problems")
def create_problem(request: Request, data=Depends(payload),
                   member: Member = Depends(PROBLEM_WRITER),
                   session: Session = Depends(get_session)):
    with bank_write(session, "该平台的外部题目已存在"):
        kp_ids = kp_ids_field(data)
        if not kp_ids:
            raise api_error(400, 40001, "请选择至少一个知识点")
        problem = apply_problem(data, session, None, member=member)
        link_problem_kps(session, problem.id, kp_ids)
        audit(request, session, "problem.created", actor_id=member.id, target_id=problem.id,
              target_type="problem", detail={"title": problem.title})
    if wants_html(request):
        return redirect(f"/problems/{problem.id}")
    return success({"id": problem.id, "title": problem.title})


@router.put("/problems/{id}")
@router.post("/problems/{id}")
def edit_problem(request: Request, id: int = Depends(object_id), data=Depends(payload),
                 member: Member = Depends(PROBLEM_WRITER),
                 session: Session = Depends(get_session)):
    with bank_write(session, "该平台的外部题目已存在"):
        problem = active_problem(session, id)
        problem = apply_problem(data, session, problem, member=member)
        kp_ids = kp_ids_field(data)
        if kp_ids is not None:
            if not kp_ids:
                raise api_error(400, 40001, "请选择至少一个知识点")
            link_problem_kps(session, problem.id, kp_ids)
        audit(request, session, "problem.updated", actor_id=member.id, target_id=problem.id,
              target_type="problem", detail={"title": problem.title})
    if wants_html(request):
        return redirect(f"/problems/{id}")
    return success({"id": problem.id, "title": problem.title})


@router.delete("/problems/{id}")
def delete_problem(request: Request, id: int = Depends(object_id),
                   member: Member = Depends(PROBLEM_WRITER),
                   session: Session = Depends(get_session)):
    with bank_write(session, "题目删除冲突"):
        problem = active_problem(session, id)
        problem.deleted_at = problem.updated_at = local_time()
        audit(request, session, "problem.deleted", actor_id=member.id, target_id=id,
              target_type="problem", detail={"title": problem.title})
    return success({"id": id, "deleted": True})


@router.get("/problems/{id}/solutions")
def list_solutions(id: int = Depends(object_id), member: Member = Depends(PROBLEM_READER),
                   session: Session = Depends(get_session)):
    active_problem(session, id)
    return success(problem_solutions(id, session))


@router.post("/problems/{id}/solutions")
def create_solution(request: Request, id: int = Depends(object_id), data=Depends(payload),
                    member: Member = Depends(PROBLEM_WRITER),
                    session: Session = Depends(get_session)):
    with bank_write(session, "题解保存冲突"):
        active_problem(session, id)
        content_md = markdown_text(data, "content_md", 20000)
        if not content_md:
            raise api_error(400, 40001, "请填写题解内容")
        content_html = validated_render(content_md)
        solution = ProblemSolution(
            problem_id=id, author_id=member.id, content_md=content_md, created_at=local_time(),
        )
        session.add(solution)
        session.flush()
        audit(request, session, "problem.solution.created", actor_id=member.id,
              target_id=solution.id, target_type="problem_solution", detail={"problem_id": id})
    if wants_html(request):
        return redirect(f"/problems/{id}")
    return success({"id": solution.id, "content_html": content_html})


# ---------------------------------------------------------------------------
# S4 训练：计划 / 阶段 / 任务 / 下发 / 打卡逐题 / 进度与预警
# ---------------------------------------------------------------------------

def training_viewer(
    request: Request,
    member: Member = Depends(authenticated),
    session: Session = Depends(get_session),
):
    """训练域读取依赖：training:read 全量视图，training:read:self 仅本人视图."""
    codes = permission_codes(member, session)
    if not (allows(codes, "training:read") or allows(codes, "training:read:self")):
        deny(request, session, target_type="training")
    return member


def training_read_all(member, session):
    return allows(permission_codes(member, session), "training:read")


def parse_day_field(data, name, label, *, required=True):
    raw = data.get(name)
    if raw in (None, ""):
        if required:
            raise api_error(400, 40001, f"请填写{label}")
        return None
    if not isinstance(raw, str):
        raise api_error(400, 40001, "参数校验失败")
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        raise api_error(400, 40001, f"{label}格式应为 YYYY-MM-DD") from None


def plan_interval(term):
    """学期区间含首尾。寒假为 1 月 1 日至 2 月末，暑假为 7 月 1 日至 8 月 31 日。"""
    match = re.fullmatch(r"([0-9]{4})-([0-9]{4})-([12])", term)
    if match:
        year1, year2 = int(match[1]), int(match[2])
        if 2000 <= year1 < year2 <= 2100 and year2 == year1 + 1:
            if match[3] == "1":
                return date(year1, 9, 1), date(year2, 1, 31)
            return date(year2, 2, 1), date(year2, 8, 31)
    match = re.fullmatch(r"([0-9]{4})-(winter|summer)", term)
    if match and 2000 <= (year := int(match[1])) <= 2100:
        if match[2] == "winter":
            return date(year, 1, 1), date(year, 2, monthrange(year, 2)[1])
        return date(year, 7, 1), date(year, 8, 31)
    raise api_error(400, 40001, "学期格式或年份无效")


def multi_ids_field(data, name):
    if isinstance(data, FormData):
        raw = data.getlist(name)
    else:
        raw = data.get(name)
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise api_error(400, 40001, "参数校验失败")
    ids = []
    for item in raw:
        try:
            ids.append(parse_object_id(str(item).strip()))
        except ValueError:
            raise api_error(400, 40001, "参数校验失败") from None
    return list(dict.fromkeys(ids))


def problem_ids_field(data):
    """题目多选：表单 getlist 保序，JSON 保序；返回去重后的题目 id 列表."""
    if isinstance(data, FormData):
        raw = data.getlist("problem_ids")
    else:
        raw = data.get("problem_ids")
    if raw in (None, ""):
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise api_error(400, 40001, "题目参数无效")
    parts = []
    for item in raw:
        for part in str(item).replace("，", ",").split(","):
            part = part.strip()
            if part:
                parts.append(part)
    ids = []
    for part in parts:
        try:
            ids.append(parse_object_id(part))
        except ValueError:
            raise api_error(400, 40001, "题目参数无效") from None
    return list(dict.fromkeys(ids))


def deadline_field(data, name="deadline_at"):
    raw = data.get(name)
    if raw in (None, ""):
        return None
    if not isinstance(raw, str):
        raise api_error(400, 40001, "参数校验失败")
    value = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, fmt)
            if fmt == "%Y-%m-%d":
                parsed = parsed.replace(hour=23, minute=59, second=59)
            return parsed.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    raise api_error(400, 40001, "截止时间格式应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM") from None


def deadline_passed(task):
    if not task.deadline_at:
        return False
    try:
        deadline = datetime.fromisoformat(deadline_field({"deadline_at": task.deadline_at}))
    except (ValueError, HTTPException):
        return False
    return datetime.now(SHANGHAI).replace(tzinfo=None, microsecond=0) > deadline


def non_negative_int_field(data, name, *, required=False):
    raw = data.get(name)
    if raw in (None, ""):
        if required:
            raise api_error(400, 40001, "参数校验失败")
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise api_error(400, 40001, "参数校验失败") from None
    if value < 0 or value > 2**31 - 1:
        raise api_error(400, 40001, "参数校验失败")
    return value


def active_training_task(session, task_id):
    task = session.get(TrainingTask, task_id)
    if task is None:
        raise api_error(404, 40401, "训练任务不存在")
    return task


def phase_item(session, phase):
    plan = session.get(TrainingPlan, phase.plan_id)
    return {
        "id": phase.id, "plan_id": phase.plan_id,
        "plan_term": plan.term if plan else None, "name": phase.name,
        "start_on": phase.start_on, "end_on": phase.end_on,
    }


def task_assignee_names(session, task_id, *, only_member_id=None):
    statement = select(Member).join(TaskAssignee, TaskAssignee.assignee_id == Member.id).where(
        TaskAssignee.task_id == task_id, TaskAssignee.assignee_type == MEMBER_ASSIGNEE,
    )
    if only_member_id is not None:
        statement = statement.where(Member.id == only_member_id)
    return [
        {"type": "会员", "id": member.id, "name": member.name, "student_no": member.student_no}
        for member in session.scalars(statement.order_by(Member.id))
    ]


def task_problem_rows(session, task_id):
    rows = session.execute(
        select(TaskProblem, Problem)
        .join(Problem, Problem.id == TaskProblem.problem_id)
        .where(TaskProblem.task_id == task_id, Problem.deleted_at.is_(None))
        .order_by(TaskProblem.sort_no, TaskProblem.problem_id)
    ).all()
    return [
        {
            "problem_id": problem.id, "title": problem.title, "difficulty": problem.difficulty,
            "problem_type": problem.problem_type, "sort_no": link.sort_no,
        }
        for link, problem in rows
    ]


def task_list_items(session, tasks):
    totals = task_totals()
    assignees = select(TaskAssignee.task_id, func.count().label("total")).where(
        TaskAssignee.assignee_type == MEMBER_ASSIGNEE
    ).group_by(TaskAssignee.task_id).subquery()
    selected = tasks.subquery()
    statement = (
        select(selected, TrainingPhase.name.label("phase_name"), TrainingPlan.id.label("plan_id"),
               TrainingPlan.term.label("plan_term"), func.coalesce(assignees.c.total, 0).label("assignee_count"),
               func.coalesce(totals.c.total, 0).label("problem_count"))
        .join(TrainingPhase, TrainingPhase.id == selected.c.phase_id)
        .join(TrainingPlan, TrainingPlan.id == TrainingPhase.plan_id)
        .outerjoin(assignees, assignees.c.task_id == selected.c.id)
        .outerjoin(totals, totals.c.task_id == selected.c.id)
        .order_by(selected.c.id.desc())
    )
    return [dict(row) for row in session.execute(statement).mappings()]


def task_detail_payload(session, task, *, member_id, read_all):
    data = task_list_items(session, select(TrainingTask).where(TrainingTask.id == task.id))[0]
    data["problems"] = task_problem_rows(session, task.id)
    data["assignees"] = task_assignee_names(session, task.id, only_member_id=None if read_all else member_id)
    data["progress"] = task_progress(session, task.id, member_id)
    return data


def count_rows(session, statement):
    return session.scalar(select(func.count()).select_from(statement.order_by(None).subquery()))


def approve_member_scope(member):
    if member.role == "dept_admin":
        return Member.dept_id == member.dept_id if member.dept_id else false()
    return true()


def training_member_scope(member):
    return active_members_query().where(approve_member_scope(member))


def training_page(request, number, page_size, has_next):
    return {
        "previous": str(request.url.include_query_params(page=number - 1, page_size=page_size)) if number > 1 else None,
        "next": str(request.url.include_query_params(page=number + 1, page_size=page_size)) if has_next else None,
    }


def threshold_from_config(session):
    raw = session.scalar(select(SysConfig.config_value).where(
        SysConfig.config_key == "training.alert_threshold"
    ))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.6
    return value if 0 <= value <= 1 else 0.6


@router.get("/training-plans")
def training_plans(request: Request, member: Member = Depends(TRAINING_READER),
                   session: Session = Depends(get_session)):
    plans = session.scalars(
        select(TrainingPlan).order_by(TrainingPlan.id.desc())
    ).all()
    counts = dict(session.execute(
        select(TrainingPhase.plan_id, func.count())
        .group_by(TrainingPhase.plan_id)
    ).all()) if plans else {}
    data = {
        "total": len(plans),
        "items": [
            {
                "id": plan.id, "term": plan.term, "goal": plan.goal,
                "intensity": plan.intensity, "created_at": plan.created_at,
                "phase_count": counts.get(plan.id, 0),
            }
            for plan in plans
        ],
    }
    if wants_html(request):
        return page(request, "training_plans.html", plans=data, intensities=PLAN_INTENSITIES)
    return success(data)


@router.post("/training-plans")
def create_training_plan(request: Request, data=Depends(payload),
                         member: Member = Depends(TRAINING_WRITER),
                         session: Session = Depends(get_session)):
    with bank_write(session, "该学期的训练计划已存在"):
        term = required_text(data, "term", "学期", 20)
        plan_interval(term)
        existing = session.scalar(select(TrainingPlan.id).where(TrainingPlan.term == term))
        if existing is not None:
            raise api_error(409, 40901, "该学期的训练计划已存在", {"id": existing})
        goal = optional_text(data, "goal", 500)
        intensity = None
        if data.get("intensity") not in (None, ""):
            intensity = enum_field(data, "intensity", PLAN_INTENSITIES, "强度")
        plan = TrainingPlan(term=term, goal=goal, intensity=intensity, created_at=local_time())
        session.add(plan)
        session.flush()
        audit(request, session, "training.plan.created", actor_id=member.id, target_id=plan.id,
              target_type="training_plan", detail={"term": term})
    if wants_html(request):
        return redirect("/training-plans")
    return success({"id": plan.id, "term": plan.term})


@router.get("/training-phases")
def training_phases(request: Request, plan_id: int | None = Query(None),
                    member: Member = Depends(TRAINING_READER),
                    session: Session = Depends(get_session)):
    statement = select(TrainingPhase)
    if plan_id is not None:
        statement = statement.where(TrainingPhase.plan_id == plan_id)
    phases = session.scalars(statement.order_by(TrainingPhase.start_on, TrainingPhase.id)).all()
    data = {"total": len(phases), "items": [phase_item(session, phase) for phase in phases]}
    if wants_html(request):
        plans = session.scalars(select(TrainingPlan).order_by(TrainingPlan.id.desc())).all()
        return page(request, "training_phases.html", phases=data, plans=plans,
                    selected_plan_id=plan_id)
    return success(data)


@router.post("/training-phases")
def create_training_phase(request: Request, data=Depends(payload),
                          member: Member = Depends(TRAINING_WRITER),
                          session: Session = Depends(get_session)):
    with bank_write(session, "训练阶段冲突"):
        plan_id = int_field(data, "plan_id", required=True)
        plan = session.get(TrainingPlan, plan_id)
        if plan is None:
            raise api_error(400, 40002, "训练计划不存在")
        name = required_text(data, "name", "阶段名称", 40)
        start_on = parse_day_field(data, "start_on", "开始日期")
        end_on = parse_day_field(data, "end_on", "结束日期")
        if start_on > end_on:
            raise api_error(400, 40001, "阶段结束日期不能早于开始日期")
        interval_start, interval_end = plan_interval(plan.term)
        if start_on < interval_start or end_on > interval_end:
            raise api_error(400, 40002, "阶段日期必须在计划区间内")
        # 阶段为闭区间，同一天不能归属同一计划的两个阶段。
        overlap = session.scalar(select(TrainingPhase.id).where(
            TrainingPhase.plan_id == plan_id,
            TrainingPhase.start_on <= end_on.isoformat(),
            TrainingPhase.end_on >= start_on.isoformat(),
        ))
        if overlap is not None:
            raise api_error(400, 40002, "训练阶段日期与已有阶段重叠", {"id": overlap})
        phase = TrainingPhase(
            plan_id=plan_id, name=name, start_on=start_on.isoformat(), end_on=end_on.isoformat(),
        )
        session.add(phase)
        session.flush()
        audit(request, session, "training.phase.created", actor_id=member.id, target_id=phase.id,
              target_type="training_phase",
              detail={"plan_id": plan_id, "name": name, "start_on": phase.start_on,
                      "end_on": phase.end_on})
    if wants_html(request):
        return redirect("/training-phases")
    return success({"id": phase.id, "name": phase.name})


@router.get("/training-tasks")
def training_tasks(request: Request, phase_id: int | None = Query(None),
                   deadline_from: str | None = Query(None), deadline_to: str | None = Query(None),
                   page_number: int = Query(1, ge=1, alias="page"), page_size: int = Query(50, ge=1, le=100),
                   member: Member = Depends(training_viewer),
                   session: Session = Depends(get_session)):
    codes = permission_codes(member, session)
    read_all = allows(codes, "training:read")
    can_write = allows(codes, "training:write")
    statement = select(TrainingTask) if read_all else member_tasks_query(member.id)
    if phase_id is not None:
        statement = statement.where(TrainingTask.phase_id == phase_id)
    for raw, lower in ((deadline_from, True), (deadline_to, False)):
        bound = parse_day_field({"date": raw}, "date", "截止日期", required=False)
        if bound:
            day = func.date(TrainingTask.deadline_at)
            statement = statement.where(day >= bound.isoformat() if lower else day <= bound.isoformat())
    total = count_rows(session, statement)
    tasks = statement.order_by(TrainingTask.id.desc()).offset((page_number - 1) * page_size).limit(page_size)
    data = {"total": total, "items": task_list_items(session, tasks), "read_all": read_all}
    if wants_html(request):
        phases = session.scalars(select(TrainingPhase).order_by(TrainingPhase.start_on)).all()
        problems = session.scalars(
            select(Problem).where(Problem.deleted_at.is_(None)).order_by(Problem.id)
        ).all() if can_write else []
        members = session.scalars(active_members_query().order_by(Member.student_no)).all() if can_write else []
        return page(request, "training_tasks.html", tasks=data, phases=phases, can_write=can_write,
                    selected_phase_id=phase_id, assignee_types=TASK_ASSIGNEE_TYPES, members=members,
                    problems=problems, pager=training_page(request, page_number, page_size, page_number * page_size < total))
    return success(data)


def selected_assignees(data, session):
    try:
        return named_assignees(session, multi_ids_field(data, "member_ids"))
    except ValueError as exc:
        raise api_error(400, 40002, str(exc)) from None


@router.post("/training-tasks")
def create_training_task(request: Request, data=Depends(payload),
                         member: Member = Depends(TRAINING_WRITER),
                         session: Session = Depends(get_session)):
    with bank_write(session, "训练任务创建冲突"):
        phase_id = int_field(data, "phase_id", required=True)
        if session.get(TrainingPhase, phase_id) is None:
            raise api_error(400, 40002, "训练阶段不存在")
        title = required_text(data, "title", "任务标题", 200)
        deadline_at = deadline_field(data)
        assignee_type = enum_field(data, "assignee_type", TASK_ASSIGNEE_TYPES, "指派类型",
                                   default="指定会员")
        problem_ids = problem_ids_field(data)
        if not problem_ids:
            raise api_error(400, 40001, "请选择至少一道题目")
        task = TrainingTask(
            phase_id=phase_id, title=title, assignee_type=assignee_type,
            deadline_at=deadline_at, status="草稿", created_by=member.id,
            created_at=local_time(),
        )
        session.add(task)
        session.flush()
        if assignee_type == "指定会员":
            for kind, member_id in selected_assignees(data, session):
                session.add(TaskAssignee(task_id=task.id, assignee_type=kind, assignee_id=member_id))
        for sort_no, problem_id in enumerate(problem_ids, start=1):
            problem = session.get(Problem, problem_id)
            if problem is None or problem.deleted_at is not None:
                raise api_error(400, 40002, "题目包中存在无效题目")
            session.add(TaskProblem(task_id=task.id, problem_id=problem_id, sort_no=sort_no))
        audit(request, session, "training.task.created", actor_id=member.id, target_id=task.id,
              target_type="training_task", detail={"title": title, "problem_ids": problem_ids})
    if wants_html(request):
        return redirect(f"/training-tasks/{task.id}")
    return success({"id": task.id, "title": task.title})


@router.get("/training-tasks/{id}")
def training_task_detail(request: Request, id: int = Depends(object_id),
                         member: Member = Depends(training_viewer),
                         session: Session = Depends(get_session)):
    task = active_training_task(session, id)
    codes = permission_codes(member, session)
    read_all = allows(codes, "training:read")
    if not read_all and not visible_to_member(session, task.id, member.id):
        audit(request, session, "auth.permission.denied", actor_id=member.id,
              target_id=task.id, target_type="training")
        session.commit()
        raise api_error(404, 40401, "训练任务不存在")
    data = task_detail_payload(session, task, member_id=member.id, read_all=read_all)
    data["read_all"] = read_all
    if wants_html(request):
        can_assign = task.status == "草稿" and allows(codes, "training:write")
        members = session.scalars(active_members_query().order_by(Member.student_no)).all() if can_assign else []
        return page(request, "task_detail.html", task=data, members=members, can_assign=can_assign,
                    assignee_types=TASK_ASSIGNEE_TYPES,
                    can_checkin=allows(codes, "checkin:write:self") and visible_to_member(session, task.id, member.id))
    return success(data)


@router.patch("/training-tasks/{id}")
def patch_training_task(request: Request, id: int = Depends(object_id), data=Depends(payload),
                        member: Member = Depends(TRAINING_WRITER),
                        session: Session = Depends(get_session)):
    with bank_write(session, "训练任务修改冲突", legacy_busy_response=False):
        task = active_training_task(session, id)
        if set(data) - {"title", "deadline_at", "csrf_token"}:
            raise api_error(400, 40001, "只能修改标题和截止时间")
        if "title" in data:
            task.title = required_text(data, "title", "任务标题", 200)
        if "deadline_at" in data:
            task.deadline_at = deadline_field(data)
        audit(request, session, "training.task.updated", actor_id=member.id, target_id=task.id,
              target_type="training_task", detail={"title": task.title, "deadline_at": task.deadline_at})
    return success({"id": task.id, "title": task.title, "deadline_at": task.deadline_at})


@router.post("/training-tasks/{id}/publish", include_in_schema=False)
@router.post("/training-tasks/{id}/assign")
def assign_training_task(request: Request, id: int = Depends(object_id), data=Depends(payload),
                         member: Member = Depends(TRAINING_WRITER),
                         session: Session = Depends(get_session)):
    with bank_write(session, "训练任务指派冲突", legacy_busy_response=False):
        task = active_training_task(session, id)
        assignee_type = enum_field(data, "assignee_type", TASK_ASSIGNEE_TYPES, "指派类型",
                                   default=task.assignee_type)
        if task.status != "草稿":
            raise api_error(400, 40002, "任务已下发，不能修改指派")
        rows = []
        if assignee_type == "指定会员":
            rows = selected_assignees(data, session) if "member_ids" in data else sorted(
                row for row in assignee_ids(session, id) if row[0] == MEMBER_ASSIGNEE
            )
            if not rows:
                raise api_error(400, 40002, "请选择指派对象")
        task.assignee_type = assignee_type
        written = publish_task(session, task, rows)
        audit(request, session, "training.task.published", actor_id=member.id, target_id=task.id,
              target_type="training_task", detail={"assignee_type": assignee_type, "assignees": written})
    if wants_html(request):
        return redirect(f"/training-tasks/{task.id}")
    return success({"id": task.id, "status": task.status, "assignee_type": assignee_type, "assignees": written})


def checkin_items_payload(session, checkins):
    items = []
    details = checkin_details(session, [checkin.id for checkin in checkins])
    for checkin in checkins:
        problems = details.get(checkin.id, [])
        solved = sum(row["solved"] for row in problems)
        items.append({
            "id": checkin.id, "task_id": checkin.task_id, "member_id": checkin.member_id,
            "checkin_date": checkin.checkin_date, "minutes": checkin.minutes,
            "stored_solved_count": checkin.solved_count, "solved_count": solved,
            "status": checkin.status, "source": checkin.source, "note": checkin.note,
            "problems": problems,
            "inconsistent": checkin.solved_count != solved,
        })
    return items


@router.get("/checkins")
def checkins_page(request: Request, task_id: int | None = Query(None), member_id: int | None = Query(None),
                  checkin_date: str | None = Query(None), date_from: str | None = Query(None),
                  date_to: str | None = Query(None),
                  page_number: int = Query(1, ge=1, alias="page"), page_size: int = Query(50, ge=1, le=100),
                  member: Member = Depends(training_viewer),
                  session: Session = Depends(get_session)):
    read_all = training_read_all(member, session)
    statement = select(Checkin)
    if read_all:
        tasks = select(TrainingTask).where(TrainingTask.published_at.isnot(None))
        if member.role == "dept_admin":
            statement = statement.where(Checkin.member_id.in_(training_member_scope(member).with_only_columns(Member.id)))
    else:
        tasks = member_tasks_query(member.id)
        statement = statement.where(Checkin.member_id == member.id)
    if task_id is not None:
        statement = statement.where(Checkin.task_id == task_id)
    if member_id is not None:
        statement = statement.where(Checkin.member_id == member_id)
    dates = {"checkin_date": checkin_date, "date_from": date_from, "date_to": date_to}
    exact, start, end = (
        parse_day_field(dates, key, "打卡日期", required=False) for key in dates
    )
    if exact:
        statement = statement.where(Checkin.checkin_date == exact.isoformat())
    if start:
        statement = statement.where(Checkin.checkin_date >= start.isoformat())
    if end:
        statement = statement.where(Checkin.checkin_date <= end.isoformat())
    selected_task = None
    if task_id is not None:
        selected_task = active_training_task(session, task_id)
        if not read_all and not visible_to_member(session, selected_task.id, member.id):
            deny(request, session, selected_task.id, "training")
    total = count_rows(session, statement)
    checkins = session.scalars(statement.order_by(Checkin.checkin_date.desc(), Checkin.id.desc())
                               .offset((page_number - 1) * page_size).limit(page_size)).all()
    data = {
        "total": total,
        "items": checkin_items_payload(session, checkins),
        "tasks": task_list_items(session, tasks),
        "read_all": read_all,
    }
    if wants_html(request):
        can_checkin = selected_task and allows(permission_codes(member, session), "checkin:write:self") \
            and visible_to_member(session, selected_task.id, member.id)
        selected_problems = task_problem_rows(session, selected_task.id) if can_checkin else []
        return page(
            request, "checkin.html", checkins=data, selected_task_id=task_id,
            selected_problems=selected_problems,
            pager=training_page(request, page_number, page_size, page_number * page_size < total),
            today=datetime.now(SHANGHAI).date().isoformat(),
        )
    return success(data)


def parse_checkin_problems(data, task, session):
    task_problem_ids = [row["problem_id"] for row in task_problem_rows(session, task.id)]
    if isinstance(data, FormData):
        checked = set(multi_ids_field(data, "solved_problem_ids"))
        if checked.difference(task_problem_ids):
            raise api_error(400, 40001, "题目不在任务有效题包中")
        return [
            {"problem_id": problem_id, "solved": 1 if problem_id in checked else 0, "attempts": None}
            for problem_id in task_problem_ids
        ]
    raw = data.get("problems")
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise api_error(400, 40001, "逐题完成情况格式无效")
    rows = {}
    active_ids = set(task_problem_ids)
    for item in raw:
        if not isinstance(item, dict):
            raise api_error(400, 40001, "逐题完成情况格式无效")
        try:
            problem_id = parse_object_id(str(item.get("problem_id", "")).strip())
        except ValueError:
            raise api_error(400, 40001, "逐题完成情况格式无效") from None
        if problem_id not in active_ids:
            raise api_error(400, 40001, "题目不在任务有效题包中")
        solved_value = item.get("solved", 0)
        if solved_value not in (0, 1, "0", "1"):
            raise api_error(400, 40001, "逐题完成情况格式无效")
        attempts = item.get("attempts")
        if attempts in ("", None):
            attempts = None
        else:
            try:
                attempts = int(str(attempts))
            except (TypeError, ValueError):
                raise api_error(400, 40001, "尝试次数无效") from None
        row = {"problem_id": problem_id, "solved": int(solved_value), "attempts": attempts}
        # 同题以已解记录优先，每题只保存一次。
        if problem_id not in rows or (row["solved"] and not rows[problem_id]["solved"]):
            rows[problem_id] = row
    return list(rows.values())


@router.post("/checkins")
def create_checkin(request: Request, data=Depends(payload),
                   member: Member = Depends(CHECKIN_WRITER),
                   session: Session = Depends(get_session)):
    with bank_write(session, "打卡保存冲突", legacy_busy_response=False):
        task_id = int_field(data, "task_id", required=True)
        task = active_training_task(session, task_id)
        if "member_id" in data and int_field(data, "member_id", required=True) != member.id:
            deny(request, session, task.id, "training")
        if not visible_to_member(session, task.id, member.id):
            deny(request, session, task.id, "training")
        day = checkin_day(data, required=False)
        if deadline_passed(task):
            raise api_error(400, 40002, "该任务已截止，请联系教练补卡")
        minutes = non_negative_int_field(data, "minutes")
        solved_count = non_negative_int_field(data, "solved_count", required=True)
        problems = parse_checkin_problems(data, task, session)
        detail_solved = sum(row["solved"] for row in problems)
        if detail_solved != solved_count:
            raise api_error(400, 40001, "已解题数与逐题明细不一致")
        checkin = Checkin(
            task_id=task.id, member_id=member.id, checkin_date=day, minutes=minutes,
            solved_count=solved_count, status="打卡", source="手工", created_at=local_time(),
        )
        session.add(checkin)
        try:
            session.flush()
        except IntegrityError as exc:
            if (getattr(exc.orig, "sqlite_errorcode", None) != sqlite3.SQLITE_CONSTRAINT_UNIQUE
                    or str(exc.orig) != "UNIQUE constraint failed: checkin.member_id, checkin.task_id, checkin.checkin_date"):
                raise
            session.rollback()
            existing_id = session.scalar(select(Checkin.id).where(
                Checkin.member_id == member.id, Checkin.task_id == task_id, Checkin.checkin_date == day,
            ))
            raise api_error(409, 40901, "今日已打卡，不能重复提交", {"existingCheckinId": existing_id}) from None
        for row in problems:
            session.add(CheckinProblem(
                checkin_id=checkin.id, problem_id=row["problem_id"],
                solved=row["solved"], attempts=row["attempts"],
            ))
        audit(request, session, "checkin.created", actor_id=member.id, target_id=checkin.id,
              target_type="checkin", detail={"task_id": task.id, "checkin_date": day,
                                             "solved_count": solved_count})
    if wants_html(request):
        return redirect(f"/training-tasks/{task.id}")
    return success({"id": checkin.id, "solved_count": solved_count})


def checkin_day(data, *, required):
    today = datetime.now(SHANGHAI).date()
    day = parse_day_field(data, "checkin_date", "打卡日期", required=required) or today
    if day > today:
        raise api_error(400, 40001, "打卡日期不能晚于今天")
    return day.isoformat()


@router.post("/checkins/makeup")
def makeup_checkin_route(request: Request, data=Depends(payload),
                         member: Member = Depends(TRAINING_APPROVER),
                         session: Session = Depends(get_session)):
    with bank_write(session, "补卡保存冲突", legacy_busy_response=False):
        task_id = int_field(data, "task_id", required=True)
        member_id = int_field(data, "member_id", required=True)
        task = active_training_task(session, task_id)
        target = session.get(Member, member_id)
        if target is None or target.deleted_at is not None or target.member_status in {"已退会", "已除名"}:
            raise api_error(400, 40002, "补卡会员不存在或已离会")
        if not visible_to_member(session, task.id, member_id):
            raise api_error(400, 40002, "任务尚未下发或未指派给该会员")
        day = checkin_day(data, required=True)
        note = optional_text(data, "note", 500)
        row, created = makeup_checkin(session, member_id, task_id, day, note, member.id)
        audit(request, session, "checkin.makeup", actor_id=member.id, target_id=row.id,
              target_type="checkin", detail={"member_id": member_id, "task_id": task_id,
                                             "checkin_date": day, "created": created, "note": note})
    if wants_html(request):
        return redirect("/checkins")
    return success({"id": row.id, "created": created, "status": row.status})


@router.get("/training-progress")
def training_progress_page(request: Request, member_id: int | None = Query(None),
                           phase_id: int | None = Query(None),
                           page_number: int = Query(1, ge=1, alias="page"), page_size: int = Query(50, ge=1, le=100),
                           member: Member = Depends(training_viewer),
                           session: Session = Depends(get_session)):
    read_all = training_read_all(member, session)
    view_all = read_all and member_id is None
    offset = (page_number - 1) * page_size
    if view_all:
        scope = training_member_scope(member).with_only_columns(Member.id)
        member_ids = list(session.scalars(scope.order_by(Member.id).offset(offset).limit(page_size + 1)))
        has_next = len(member_ids) > page_size
        items = member_summaries(session, member_ids[:page_size], phase_id)
    else:
        target_id = member_id if member_id is not None else member.id
        if (not read_all and target_id != member.id) or (
            member.role == "dept_admin" and session.scalar(
                training_member_scope(member).with_only_columns(Member.id).where(Member.id == target_id)
            ) is None
        ):
            deny(request, session, target_id, "training")
        tasks = member_tasks_query(target_id)
        if phase_id is not None:
            tasks = tasks.where(TrainingTask.phase_id == phase_id)
        has_next = page_number * page_size < count_rows(session, tasks)
        items = member_summaries(session, [target_id], phase_id, task_offset=offset, task_limit=page_size)
    data = {
        "items": items, "view_all": view_all, "read_all": read_all, "phase_id": phase_id,
        "total": sum(item["total"] for item in items), "solved": sum(item["solved"] for item in items),
        "has_next": has_next,
    }
    if wants_html(request):
        phases = session.scalars(select(TrainingPhase).order_by(TrainingPhase.start_on)).all()
        return page(request, "training_progress.html", progress=data, phases=phases,
                    selected_phase_id=phase_id, pager=training_page(request, page_number, page_size, has_next))
    return success(data)


@router.get("/training-alerts")
def training_alerts(request: Request, phase_id: int | None = Query(None),
                    page_number: int = Query(1, ge=1, alias="page"), page_size: int = Query(50, ge=1, le=100),
                    member: Member = Depends(TRAINING_READER),
                    session: Session = Depends(get_session)):
    threshold = threshold_from_config(session)
    scope = training_member_scope(member).with_only_columns(Member.id)
    statement = alert_query(phase_id, threshold, member_ids=scope)
    total = count_rows(session, statement)
    items = alert_items(session, statement.offset((page_number - 1) * page_size).limit(page_size))
    has_next = page_number * page_size < total
    data = {"threshold": threshold, "total": total, "items": items, "has_next": has_next}
    if wants_html(request):
        phases = session.scalars(select(TrainingPhase).order_by(TrainingPhase.start_on)).all()
        return page(request, "training_alerts.html", alerts=data, phases=phases,
                    selected_phase_id=phase_id, pager=training_page(request, page_number, page_size, has_next))
    return success(data)


# ---------------------------------------------------------------------------
# S5 考核与转正：周测录分、考核计算、转正、退会与除名
# ---------------------------------------------------------------------------

ASSESSMENT_TEST_TYPES = ("周测", "月测")
ASSESSMENT_READER = require_permission("member:read", "member:read:self", target_type="assessment")
ASSESSMENT_RUNNER = require_permission("system:admin", target_type="assessment")
MEMBER_APPROVER = require_permission("member:approve", target_type="member")


def active_assessment_test(session, test_id):
    test = session.get(AssessmentTest, test_id)
    if test is None:
        raise api_error(404, 40401, "测评不存在")
    return test


def assessment_test_payload(test):
    return {
        "id": test.id, "title": test.title, "test_type": test.test_type,
        "held_on": test.held_on, "created_at": test.created_at,
    }


def assessment_score_rows(data):
    if isinstance(data, FormData):
        rows = []
        for member_id in multi_ids_field(data, "member_ids"):
            raw_solved = data.get(f"solved_{member_id}")
            raw_score = data.get(f"score_{member_id}")
            if (raw_solved, raw_score) == (data.get(f"old_solved_{member_id}", ""), data.get(f"old_score_{member_id}", "")):
                continue
            rows.append({"member_id": member_id, "solved_count": raw_solved, "score": raw_score,
                         "version": data.get(f"version_{member_id}")})
        return rows
    raw = data.get("rows")
    if raw in (None, ""):
        raise api_error(400, 40001, "请提交成绩数据")
    if not isinstance(raw, list) or not raw:
        raise api_error(400, 40001, "请提交至少一条成绩")
    return raw


def assessment_items_payload(session, filters, pagination):
    statement = (
        select(MemberAssessment, Member)
        .join(Member, Member.id == MemberAssessment.member_id)
        .where(*filters)
        .order_by(MemberAssessment.term.desc(), MemberAssessment.assessed_at.desc(), Member.id)
    )
    items = []
    rows, metadata = s5_page(session, statement, pagination)
    for assessment, target in rows:
        items.append({
            "id": assessment.id, "member_id": target.id, "student_no": target.student_no,
            "name": target.name, "term": assessment.term, "result": assessment.result,
            "in_watchlist": assessment.in_watchlist, "assessed_at": assessment.assessed_at,
            "remark": assessment.remark,
        })
    return items, metadata


def s5_pagination(request: Request, number: int = Query(1, alias="page", ge=1, le=2**63 // 100),
                  size: int = Query(50, alias="page_size", ge=1, le=100)):
    return request, number, size


def s5_page(session, statement, pagination):
    request, number, size = pagination
    total = count_rows(session, statement)
    rows = session.execute(statement.offset((number - 1) * size).limit(size)).all()
    return rows, {"total": total, "page": number, "page_size": size,
                  "links": training_page(request, number, size, number * size < total)}


def s5_read_scope(request, member, session, permission):
    if request.query_params.get("scope") == "mine":
        codes = permission_codes(member, session)
        if any(allows(codes, code) for code in ("member:read:self", "member:read", permission)):
            if "member_id" in request.query_params and request.query_params["member_id"] != str(member.id):
                deny(request, session)
            return Member.id == member.id
    if not allows(permission_codes(member, session), permission):
        deny(request, session)
    return approve_member_scope(member)


def s5_target(session, actor, target_id):
    if session.scalar(select(Member.id).where(Member.id == target_id, approve_member_scope(actor))) is None:
        raise api_error(404, 40401, "会员不存在")


@router.get("/assessment-tests")
def assessment_tests_page(request: Request, member: Member = Depends(TRAINING_READER),
                          pagination=Depends(s5_pagination), session: Session = Depends(get_session)):
    rows, metadata = s5_page(session, select(AssessmentTest).order_by(
        AssessmentTest.held_on.desc(), AssessmentTest.id.desc(),
    ), pagination)
    data = {**metadata, "items": [assessment_test_payload(test) for (test,) in rows]}
    if wants_html(request):
        can_score = allows(permission_codes(member, session), "training:approve")
        return page(request, "assessment_tests.html", tests=data, can_score=can_score,
                    test_types=ASSESSMENT_TEST_TYPES,
                    can_create=can_score and member.role in {"coach", "sys_admin"})
    return success(data)


@router.post("/assessment-tests")
def create_assessment_test(request: Request, data=Depends(payload),
                           member: Member = Depends(TRAINING_APPROVER),
                           session: Session = Depends(get_session)):
    if member.role not in {"coach", "sys_admin"}:
        deny(request, session)
    with bank_write(session, "测评保存冲突", legacy_busy_response=False):
        title = required_text(data, "title", "标题", 120)
        test_type = enum_field(data, "test_type", ASSESSMENT_TEST_TYPES, "类型")
        held_on = parse_day_field(data, "held_on", "测评日期")
        test = AssessmentTest(
            title=title, test_type=test_type, held_on=held_on.isoformat(),
            created_at=local_time(),
        )
        session.add(test)
        session.flush()
        audit(request, session, "assessment.test.created", actor_id=member.id, target_id=test.id,
              target_type="assessment_test",
              detail={"title": title, "test_type": test_type, "held_on": test.held_on})
    if wants_html(request):
        return redirect("/assessment-tests")
    return success(assessment_test_payload(test))


@router.get("/assessment-tests/{id}/scores")
def assessment_scores_page(request: Request, id: int = Depends(object_id),
                           member: Member = Depends(TRAINING_APPROVER),
                           pagination=Depends(s5_pagination), session: Session = Depends(get_session)):
    test = active_assessment_test(session, id)
    rows, metadata = s5_page(session, training_member_scope(member).order_by(Member.student_no), pagination)
    members = [target for (target,) in rows]
    existing = {
        row.member_id: row
        for row in session.scalars(select(TestScore).where(
            TestScore.test_id == id, TestScore.member_id.in_([target.id for target in members]),
        ))
    }
    items = []
    for target in members:
        score = existing.get(target.id)
        items.append({
            "id": target.id, "student_no": target.student_no, "name": target.name,
            "solved_count": score.solved_count if score else None,
            "score": score.score if score else None,
            "version": score.version if score else 0,
        })
    data = {**metadata, "test": assessment_test_payload(test), "items": items}
    if wants_html(request):
        return page(request, "assessment_scores.html", scores=data)
    return success(data)


@router.post("/assessment-tests/{id}/scores")
def submit_assessment_scores(request: Request, id: int = Depends(object_id),
                             data=Depends(payload),
                             member: Member = Depends(TRAINING_APPROVER),
                             session: Session = Depends(get_session)):
    with bank_write(session, "成绩录入冲突", legacy_busy_response=False):
        test = active_assessment_test(session, id)
        rows = assessment_score_rows(data)
        try:
            result = record_scores(session, test.id, rows, member.id, approve_member_scope(member))
        except AssessmentError as exc:
            raise api_error(exc.status, exc.code, str(exc)) from None
        audit(request, session, "assessment.scores.recorded", actor_id=member.id,
              target_id=test.id, target_type="assessment_test",
              detail={"test_id": test.id, **result})
    if wants_html(request):
        return redirect(f"/assessment-tests/{id}/scores")
    return success(result)


@router.get("/assessments")
def assessments_page(request: Request, term: str | None = Query(None),
                     member: Member = Depends(ASSESSMENT_READER),
                     pagination=Depends(s5_pagination), session: Session = Depends(get_session)):
    scope = s5_read_scope(request, member, session, "member:read")
    filters = [Member.deleted_at.is_(None), scope]
    if term:
        filters.append(MemberAssessment.term == term)
    items, metadata = assessment_items_payload(session, filters, pagination)
    data = {**metadata, "items": items, "term": term or ""}
    if wants_html(request):
        can_run = allows(permission_codes(member, session), "system:admin")
        jobs = session.scalars(
            select(JobRun).order_by(JobRun.started_at.desc(), JobRun.id.desc()).limit(20)
        ).all() if can_run else []
        return page(request, "assessments.html", assessments=data, jobs=jobs, can_run=can_run)
    return success(data)


@router.post("/assessments/run")
def run_assessment(request: Request, data=Depends(payload),
                   member: Member = Depends(ASSESSMENT_RUNNER),
                   session: Session = Depends(get_session)):
    term = required_text(data, "term", "学期", 20)
    try:
        term_interval(term)
    except ValueError as exc:
        raise api_error(400, 40001, str(exc)) from None
    biz_date = datetime.now(SHANGHAI).date().isoformat()
    try:
        result = run_job(session, "assess", biz_date, lambda s: run_assess_term(s, term),
                         term=term, request_id=request.state.request_id)
    except JobFailed:
        raise api_error(500, 50001, "", {"request_id": request.state.request_id}) from None
    audit(request, session, "assessment.run", actor_id=member.id, target_type="assessment",
          detail={"term": term, **result})
    session.commit()
    if wants_html(request):
        return redirect("/assessments")
    return success(result)


@router.get("/promotions")
def promotions_page(request: Request, term: str | None = Query(None),
                    member: Member = Depends(authenticated),
                    pagination=Depends(s5_pagination), session: Session = Depends(get_session)):
    scope = s5_read_scope(request, member, session, "member:approve")
    mine = request.query_params.get("scope") == "mine"
    active_term = current_term(session)
    selected_term = term or active_term
    try:
        term_interval(selected_term)
    except ValueError as exc:
        raise api_error(400, 40001, str(exc)) from None
    statement = select(Member).where(Member.deleted_at.is_(None), scope).order_by(Member.student_no)
    if not mine:
        statement = statement.where(Member.member_status == "观察会员")
    rows, metadata = s5_page(session, statement, pagination)
    items = promotion_items(session, [target for (target,) in rows], selected_term)
    for item in items:
        if selected_term != active_term:
            item["ready"] = False
            item["reasons"].append("历史学期只读")
    data = {**metadata, "term": selected_term, "items": items, "mine": mine}
    if wants_html(request):
        return page(request, "promotions.html", promotions=data)
    return success(data)


@router.post("/members/{id}/promote")
def promote_member(request: Request, id: int = Depends(object_id), data=Depends(payload),
                   member: Member = Depends(MEMBER_APPROVER),
                   session: Session = Depends(get_session)):
    with bank_write(session, "转正冲突", legacy_busy_response=False):
        try:
            s5_target(session, member, id)
            promote(session, id, member.id, data.get("term") or request.query_params.get("term"))
        except AssessmentError as exc:
            raise api_error(exc.status, exc.code, str(exc)) from None
    if wants_html(request):
        return redirect("/promotions")
    return success({"id": id, "member_status": "正式会员", "member_type": "正式会员"})


def close_pending_leave(session, member_id, reviewer_id, status, note):
    result = session.execute(update(MemberApplication).where(
        MemberApplication.member_id == member_id, MemberApplication.apply_type == "退会",
        MemberApplication.status == "待审核", MemberApplication.active_flag == "1",
    ).values(status=status, active_flag=None, review_note=note,
             reviewed_by=reviewer_id, reviewed_at=local_time()))
    return result.rowcount


@router.post("/members/{id}/leave")
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


@router.post("/members/{id}/leave/approve")
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


@router.post("/members/{id}/withdraw")
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


@router.post("/members/{id}/expel")
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


# ---------------------------------------------------------------------------
# S6 档案与统计：档案页、CSV 导出、奖惩、会员统计
# ---------------------------------------------------------------------------

AWARD_TYPES = ("奖励", "惩处")
AWARD_APPROVER = require_permission("member:approve", target_type="award")


def signed_int_field(data, name):
    raw = data.get(name)
    if raw in (None, ""):
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise api_error(400, 40001, "参数校验失败") from None
    if not -2**31 <= value <= 2**31 - 1:
        raise api_error(400, 40001, "参数校验失败")
    return value


def award_item(row, target, actor, codes):
    return {
        **award_fields(row, show_reason=can_view_sensitive(actor, target, codes)),
        "student_no": target.student_no, "name": target.name,
    }


def profile_history_page(request, parameter, number, rows):
    return {
        "previous": str(request.url.include_query_params(**{parameter: number - 1})) if number > 1 else None,
        "next": str(request.url.include_query_params(**{parameter: number + 1})) if len(rows) > 50 else None,
    }


def profile_target(request, session, actor, target_id):
    scope = member_scope(request, actor, session, target_id)
    row = session.execute(
        select(Member, Department.name).outerjoin(Department).where(
            Member.id == target_id, Member.deleted_at.is_(None), scope,
        )
    ).one_or_none()
    if row is None:
        if actor.role == "dept_admin" and target_id != actor.id:
            deny(request, session, target_id)
        raise HTTPException(404, "会员不存在")
    return row


@router.get("/members/{id}/profile")
def member_profile(request: Request, id: int = Depends(object_id),
                   assessment_page: int = Query(1, ge=1, le=2**63 // 100),
                   award_page: int = Query(1, ge=1, le=2**63 // 100),
                   member: Member = Depends(authenticated),
                   session: Session = Depends(get_session)):
    target, department = profile_target(request, session, member, id)
    training = training_summary(session, id)
    training["rate_text"] = f"{training['rate'] * 100:.2f}%"
    codes = permission_codes(member, session)
    assessments = assessment_history(session, id, offset=(assessment_page - 1) * 50, limit=51)
    awards = awards_of(
        session, id, show_reason=can_view_sensitive(member, target, codes),
        offset=(award_page - 1) * 50, limit=51,
    )
    summary = award_summary(session, id)
    data = {
        "id": target.id, "student_no": target.student_no, "name": target.name,
        "department": department, "major": target.major, "grade": target.grade,
        "member_type": target.member_type, "member_status": target.member_status,
        "joined_on": target.joined_on, "left_on": target.left_on,
        "training": training, "assessments": assessments[:50], "awards": awards[:50],
        "assessment_pagination": profile_history_page(request, "assessment_page", assessment_page, assessments),
        "award_pagination": profile_history_page(request, "award_page", award_page, awards),
        "award_summary": summary, "award_summary_points": format_points(summary),
        "contest_note": "赛事模块随 D 域交付",
    }
    for field in ("email", "phone"):
        value = mask_contact(getattr(target, field), member, target, codes)
        if value is not None or can_view_sensitive(member, target, codes):
            data[field] = value
    if wants_html(request):
        return page(request, "profile.html", profile=data)
    return success(data)


@router.get("/members/{id}/export")
def export_member_csv(request: Request, id: int = Depends(object_id),
                      format: str = Query("csv"), term: str | None = Query(None),
                      member: Member = Depends(authenticated),
                      session: Session = Depends(get_session)):
    if format != "csv":
        raise api_error(400, 40001, "仅支持 CSV 导出")
    target, department = profile_target(request, session, member, id)
    try:
        interval = term_interval(term) if term is not None else None
    except ValueError as exc:
        raise api_error(400, 40001, str(exc)) from None
    filename = csv_filename_for(target)
    sections = profile_csv_sections(
        session, [(target, department)], member, permission_codes(member, session),
        include_term_summary=True, term=term, interval=interval,
    )
    content = ("\ufeff" + "".join(render_csv_sections(sections))).encode("utf-8")
    return Response(
        content=content, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/awards")
def award_list(request: Request, page_number: int = Query(1, alias="page", ge=1, le=2**63 // 100),
               page_size: int = Query(50, ge=1, le=100),
               scope: str | None = Query(None),
               option_page: int = Query(1, ge=1, le=2**63 // 100),
               member: Member = Depends(authenticated),
               session: Session = Depends(get_session)):
    codes = permission_codes(member, session)
    can_read_all = allows(codes, "member:read")
    if not can_read_all and not allows(codes, "member:read:self"):
        deny(request, session, target_type="award")
    mine = scope == "mine" or not can_read_all
    member_filter = Member.id == member.id if mine else approve_member_scope(member)
    statement = (
        select(MemberAward, Member)
        .join(Member, Member.id == MemberAward.member_id)
        .where(member_filter, Member.deleted_at.is_(None))
        .order_by(MemberAward.happened_on.desc(), MemberAward.id.desc())
    )
    total = count_rows(session, statement)
    rows = session.execute(statement.offset((page_number - 1) * page_size).limit(page_size))
    items = [award_item(row, target, member, codes) for row, target in rows]
    data = {"total": total, "page": page_number, "page_size": page_size, "items": items}
    if wants_html(request):
        can_approve = scope != "mine" and allows(codes, "member:approve")
        member_options = []
        if can_approve:
            member_options = session.scalars(
                active_members_query().where(approve_member_scope(member)).order_by(Member.student_no)
                .offset((option_page - 1) * 50).limit(51)
            ).all()
        return page(
            request, "awards.html", awards=data, can_approve=can_approve,
            mine=mine, can_read_all=can_read_all,
            member_options=member_options[:50], award_types=AWARD_TYPES,
            option_pagination=profile_history_page(request, "option_page", option_page, member_options),
            pagination=training_page(request, page_number, page_size, page_number * page_size < total),
        )
    return success(data)


@router.post("/awards")
def create_award(request: Request, data=Depends(payload),
                 member: Member = Depends(AWARD_APPROVER),
                 session: Session = Depends(get_session)):
    with bank_write(session, "同会员同一天同标题的奖惩已存在", legacy_busy_response=False):
        member_id = int_field(data, "member_id", required=True)
        target = session.scalar(select(Member).where(Member.id == member_id, approve_member_scope(member)))
        if target is None:
            raise HTTPException(404, "会员不存在")
        if target.deleted_at is not None or target.member_status in LEFT_STATUSES:
            raise api_error(400, 40002, "会员已离会或删除")
        award_type = enum_field(data, "award_type", AWARD_TYPES, "奖惩类型")
        title = required_text(data, "title", "标题", 100)
        if len(title) < 2:
            raise api_error(400, 40001, "标题须为 2–100 个字符")
        reason = optional_text(data, "reason", 500)
        happened_on = parse_day_field(data, "happened_on", "发生日期")
        today = datetime.now(SHANGHAI).date()
        if happened_on > today:
            raise api_error(400, 40001, "发生日期不能晚于今天")
        extra_points = signed_int_field(data, "extra_points")
        duplicate = session.scalar(select(MemberAward.id).where(
            MemberAward.member_id == member_id,
            MemberAward.happened_on == happened_on.isoformat(),
            MemberAward.title == title,
        ))
        if duplicate is not None:
            raise api_error(409, 40901, "同会员同一天同标题的奖惩已存在", {"id": duplicate})
        award = MemberAward(
            member_id=member_id, award_type=award_type, title=title, reason=reason,
            happened_on=happened_on.isoformat(), extra_points=extra_points,
            created_at=local_time(),
        )
        session.add(award)
        session.flush()
        audit(request, session, "award.created", actor_id=member.id, target_id=award.id,
              target_type="member_award",
              detail={"member_id": member_id, "award_type": award_type, "title": title,
                      "happened_on": award.happened_on, "extra_points": extra_points})
    if wants_html(request):
        return redirect(f"/members/{member_id}/profile")
    return success(award_item(award, target, member, permission_codes(member, session)))


@router.get("/stats/members")
def stats_members(request: Request, term: str | None = Query(None),
                  member: Member = Depends(authenticated),
                  session: Session = Depends(get_session)):
    if not allows(permission_codes(member, session), "member:read"):
        deny(request, session, target_type="member")
    scope = approve_member_scope(member)
    department_scope = true()
    if member.role == "dept_admin":
        department_scope = Department.id == member.dept_id
    try:
        stats = member_stats(session, term, scope=scope, department_scope=department_scope)
    except ValueError as exc:
        raise api_error(400, 40001, str(exc)) from None
    department = session.get(Department, member.dept_id) if member.dept_id else None
    data = {
        **stats,
        "scope": "本部门" if member.role == "dept_admin" else "全协会",
        "department": department.name if department else None,
    }
    if wants_html(request):
        return page(request, "member_stats.html", stats=data)
    return success(data)
