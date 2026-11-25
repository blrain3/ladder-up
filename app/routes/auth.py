# 登录、登出、会话和密码管理。

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import (
    Response,
)
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..auth import (
    active_member,
    audit,
    authenticated,
    client_ip,
    permission_codes,
    unauthorized,
)
from ..db import get_session
from ..models import (
    Department,
    Member,
)
from ..models.base import local_time
from ..security import (
    create_session,
    hash_password,
    load_session,
    revoke_all_sessions,
    revoke_session,
    rotate_session,
    valid_password,
    verify_password,
)
from ..web import page, success, wants_html
from ._common import invalid, payload, redirect
from .members import member_data

router = APIRouter()
password_router = APIRouter()


def set_session_cookie(request, response, token, ttl):
    response.set_cookie(
        "sid", token, max_age=ttl * 3600, path="/", httponly=True,
        secure=request.app.state.settings.cookie_secure, samesite="lax",
    )
    return response


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
    previous_hash = member.password_hash if member is not None and active_member(member) else None
    valid = previous_hash and isinstance(password, str) and verify_password(password, previous_hash)
    if valid:
        # Recheck the credential after acquiring the write lock to serialize with password changes.
        session.rollback()
        session.execute(text("BEGIN IMMEDIATE"))
        member = session.get(Member, member_id)
        valid = member is not None and active_member(member) and member.password_hash == previous_hash
    if not valid:
        message = "学号或密码错误"
        audit(request, session, "auth.login.failed", target_id=member_id)
        session.commit()
        if wants_html(request):
            return page(request, "login.html", status=401, error_code=40101, error_message=message)
        raise HTTPException(401, message)

    assert member is not None  # A valid login has a member after the locked recheck.
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


@password_router.get("/me")
def me(request: Request, member: Member = Depends(authenticated),
       session: Session = Depends(get_session)):
    department = session.scalar(select(Department.name).where(Department.id == member.dept_id))
    return success(member_data(member, department) | {
        "role": member.role, "permissions": sorted(permission_codes(member, session)),
        "csrf_token": request.state.csrf_token,
    })


@password_router.get("/me/password")
def password_page(request: Request, _member: Member = Depends(authenticated)):
    return page(request, "password.html")


@password_router.put("/me/password")
@password_router.post("/me/password")
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
    refreshed_member = session.get(Member, member_id)
    if refreshed_member is None or not active_member(refreshed_member) or not load_session(sid, session=session):
        raise unauthorized()
    member = refreshed_member
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
