import secrets

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select, text, true
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from .auth import (
    active_member, allows, audit, authenticated, client_ip, csrf_form, csrf_matches, deny,
    permission_codes, require_permission, unauthorized,
)
from .db import get_session
from .models import Department, Member, MemberApplication
from .models.base import local_time
from .security import (
    create_session, hash_password, load_session, revoke_all_sessions, revoke_session,
    rotate_session, valid_password, verify_password,
)
from .web import content_type, page, success, wants_html


router = APIRouter()


def set_session_cookie(response, token, ttl):
    response.set_cookie(
        "sid", token, max_age=ttl * 3600, path="/", httponly=True, secure=True, samesite="lax",
    )
    return response


def redirect(path):
    return RedirectResponse(path, status_code=303)


def invalid(message="请检查填写的内容"):
    return HTTPException(400, message)


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
    return set_session_cookie(redirect("/"), token, ttl)


@router.post("/auth/logout")
def logout(request: Request, member: Member = Depends(authenticated),
           session: Session = Depends(get_session)):
    revoke_session(request.cookies["sid"], session=session)
    audit(request, session, "auth.logout", actor_id=member.id, target_id=member.id)
    session.commit()
    response = redirect("/login") if wants_html(request) else Response(status_code=204)
    response.delete_cookie("sid", path="/", httponly=True, secure=True, samesite="lax")
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
def member_detail(request: Request, id: int = Path(ge=1, le=2**63 - 1),
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
    }
    if wants_html(request):
        return page(request, "member.html", profile=data)
    return success(data)


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
    member.password_hash = replacement_hash
    member.updated_at = local_time()
    ttl = request.app.state.settings.session_ttl_hours
    revoke_all_sessions(member.id, sid, "password_change", session=session)
    token = rotate_session(sid, session=session, ttl_hours=ttl, reason="password_change")
    if token is None:
        raise unauthorized()
    audit(request, session, "auth.password.changed", actor_id=member.id, target_id=member.id)
    session.commit()

    response = redirect("/members") if wants_html(request) else Response(status_code=204)
    return set_session_cookie(response, token, ttl)


@router.get("/apply")
def apply_page(request: Request):
    token = secrets.token_urlsafe(32)
    response = page(request, "apply.html", csrf_token=token)
    response.set_cookie(
        "csrf_anon", token, max_age=1800, path="/", httponly=True,
        secure=request.url.scheme == "https", samesite="lax",
    )
    return response


@router.post("/apply")
def submit_application(request: Request, form=Depends(csrf_form),
                       session: Session = Depends(get_session)):
    tokens = form.getlist("csrf_token")
    if len(tokens) != 1 or not csrf_matches(request.cookies.get("csrf_anon"), tokens[0]):
        raise HTTPException(400, {"code": 40002, "message": "安全令牌无效，请重新打开申请页"})
    student_no = field(form, "student_no", 20, required=True)
    grade = field(form, "grade", 4)
    if grade and not (grade.isascii() and grade.isdecimal() and len(grade) == 4):
        raise invalid("入学年份须为四位数字")
    statement = insert(MemberApplication).values(
        student_no=student_no, name=field(form, "name", 50, required=True),
        major=field(form, "major", 80), grade=int(grade) if grade else None,
        phone=field(form, "phone", 20), email=field(form, "email", 120),
        apply_type="入会", status="待审核", active_flag="1", created_at=local_time(),
    ).on_conflict_do_nothing(index_elements=["student_no", "apply_type", "active_flag"])
    if session.execute(statement).rowcount == 0:
        existing_id = session.scalar(select(MemberApplication.id).where(
            MemberApplication.student_no == student_no,
            MemberApplication.apply_type == "入会", MemberApplication.active_flag == "1",
        ))
        message = "该学号已有待处理的入会申请"
        if wants_html(request):
            return page(request, "error.html", status=409, error_code=40901, error_message=message)
        return JSONResponse(
            {"code": 40901, "message": message, "data": {"id": existing_id}}, status_code=409,
        )
    session.commit()
    return redirect("/apply/success")


@router.get("/apply/success")
def application_success(request: Request):
    return page(request, "apply_success.html")


@router.post("/member-applications/{id}/review")
def review_application(id: int, _member: Member = Depends(require_permission("member:approve"))):
    # S2 supplies the review workflow behind this authorization boundary.
    raise HTTPException(501, "申请审核将在入会阶段开放")
