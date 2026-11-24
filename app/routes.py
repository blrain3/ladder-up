import os
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from python_multipart.exceptions import MultipartParseError
from sqlalchemy import func, select, text, true, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile
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
from .models import Attachment, Department, Member, MemberApplication
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
from .web import content_type, page, parse_object_id, success, wants_html

router = APIRouter()

STUDENT_NO_RE = re.compile(r"^[0-9]{8,20}$")
PHONE_RE = re.compile(r"^[0-9]{11}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
APPLICATION_STATUSES = ("待审核", "待面试", "已面试", "已通过", "已拒绝", "已撤回")
APPLICATION_APPROVER = require_permission("member:approve", target_type="application")
APPLICATION_READER = require_permission("member:approve", "member:read", target_type="application")


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
