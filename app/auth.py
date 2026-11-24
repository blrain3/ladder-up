import json
from hmac import compare_digest, digest

from fastapi import Depends, HTTPException, Request
from python_multipart.exceptions import MultipartParseError
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData
from starlette.exceptions import HTTPException as StarletteHTTPException

from .db import get_session
from .models import AuditLog, Member, MemberPermission, PermissionPoint
from .models.base import local_time
from .security import load_session, revoke_session
from .web import content_type, parse_object_id

ROLE_PERMISSIONS = {
    "member": {"member:read:self", "problem:read", "training:read:self", "checkin:write:self"},
    "dept_admin": {"member:read", "member:write", "training:read", "training:write", "problem:read"},
    "coach": {"member:read", "member:approve", "problem:read", "problem:write",
              "training:approve", "contest:*"},
}


def client_ip(request):
    return request.client.host if request.client else None


def audit(request, session, action, *, actor_id=None, target_id=None, target_type="member", detail=None):
    session.add(AuditLog(
        request_id=request.state.request_id, actor_id=actor_id, action=action,
        target_type=target_type, target_id=target_id, ip=client_ip(request), created_at=local_time(),
        after_json=json.dumps(detail, ensure_ascii=False) if detail is not None else None,
    ))


def unauthorized():
    return HTTPException(401, "请登录后重试")


def active_member(member):
    return (
        member is not None and member.deleted_at is None
        and member.member_status not in {"已退会", "已除名"}
    )


def current_member(request: Request, session: Session = Depends(get_session)):
    token = request.cookies.get("sid")
    current = load_session(token, session=session)
    member = session.get(Member, current.member_id) if current else None
    if not active_member(member):
        raise unauthorized()
    request.state.member_id = member.id
    request.state.member = member
    request.state.csrf_token = digest(token.encode(), current.csrf_nonce.encode(), "sha256").hex()
    return member


def permission_codes(member, session):
    statement = select(PermissionPoint.code)
    if member.role == "sys_admin":
        return set(session.scalars(statement)) | {"*"}
    grants = statement.join(MemberPermission).where(MemberPermission.member_id == member.id)
    return ROLE_PERMISSIONS.get(member.role, set()) | set(session.scalars(grants))


def allows(codes, code):
    return "*" in codes or code in codes or f"{code.split(':', 1)[0]}:*" in codes


def deny(request, session, target_id=None, target_type="member"):
    audit(
        request, session, "auth.permission.denied", actor_id=request.state.member_id,
        target_id=target_id, target_type=target_type,
    )
    session.commit()
    raise HTTPException(403, "无权访问此资源")


def csrf_matches(expected, presented):
    return (
        bool(expected) and isinstance(presented, str) and bool(presented)
        and compare_digest(expected.encode(), presented.encode())
    )


async def csrf_form(request: Request):
    try:
        return await request.form()
    except (StarletteHTTPException, MultipartParseError):
        return FormData()


async def validate_csrf(request, session, form=None):
    media_type = content_type(request)
    if media_type == "application/json" or (request.method == "DELETE" and not media_type):
        presented = request.headers.get("X-CSRF-Token")
    else:
        if form is None:
            form = await csrf_form(request)
        tokens = form.getlist("csrf_token")
        presented = tokens[0] if len(tokens) == 1 else None
    if not csrf_matches(request.state.csrf_token, presented):
        revoke_session(request.cookies["sid"], session=session, reason="csrf")
        session.commit()
        request.state.member = None
        request.state.csrf_token = None
        raise HTTPException(400, {"code": 40002, "message": "安全令牌无效，请重新登录"})


async def authenticated(
    request: Request,
    member: Member = Depends(current_member),
    session: Session = Depends(get_session),
):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        await validate_csrf(request, session)
    return member


def require_permission(code, *alternatives, target_type=None):
    def check(
        request: Request,
        member: Member = Depends(authenticated),
        session: Session = Depends(get_session),
    ):
        codes = permission_codes(member, session)
        if not any(allows(codes, candidate) for candidate in (code, *alternatives)):
            raw_id = request.path_params.get("id")
            target_id = None
            if raw_id is not None:
                try:
                    target_id = parse_object_id(raw_id)
                except ValueError:
                    raise HTTPException(400, "参数校验失败") from None
            deny(request, session, target_id, target_type or code.split(":", 1)[0])
        return member
    return check
