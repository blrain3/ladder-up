import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from python_multipart.exceptions import MultipartParseError
from sqlalchemy import delete, false, func, inspect, or_, select, text, true, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

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
from .db import get_session
from .markdown import render
from .models import (
    Attachment,
    Department,
    KnowledgePoint,
    Member,
    MemberApplication,
    Problem,
    ProblemKp,
    ProblemSolution,
)
from .models.base import local_time
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
        return page(request, "member.html", profile=data)
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
    filters = []
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
def bank_write(session, conflict_message):
    try:
        session.execute(text("BEGIN IMMEDIATE"))
        yield
        session.commit()
    except OperationalError as exc:
        session.rollback()
        sqlite_code = getattr(exc.orig, "sqlite_errorcode", 0) & 0xff
        if sqlite_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
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
