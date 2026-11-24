from datetime import datetime, timedelta, timezone
from hashlib import sha256
import secrets
from zoneinfo import ZoneInfo

from passlib.hash import bcrypt
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import MemberSession
from .models.base import local_time


def valid_password(password: str) -> bool:
    try:
        size = len(password.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    return 0 < size <= 72 and "\0" not in password


def hash_password(password: str) -> str:
    if not valid_password(password):
        raise ValueError("密码须为 1–72 个 UTF-8 字节，且不能含空字符")
    return bcrypt.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    if not valid_password(password):
        return False
    try:
        return bcrypt.verify(password, password_hash)
    except ValueError:
        return False


def token_digest(token: str) -> str:
    return sha256(token.encode()).hexdigest()


def create_session(member_id, ua, ip, *, session: Session, ttl_hours=8):
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    local = ZoneInfo("Asia/Shanghai")
    format_time = "%Y-%m-%d %H:%M:%S"
    session.add(MemberSession(
        member_id=member_id,
        token_hash=token_digest(token),
        csrf_nonce=secrets.token_urlsafe(32),
        issued_at=now.astimezone(local).strftime(format_time),
        expires_at=(now + timedelta(hours=ttl_hours)).astimezone(local).strftime(format_time),
        user_agent=ua[:200] if ua else None,
        ip=ip,
    ))
    return token


def load_session(token, *, session: Session):
    if not token:
        return None
    return session.scalar(select(MemberSession).where(
        MemberSession.token_hash == token_digest(token),
        MemberSession.revoked_at.is_(None),
        MemberSession.expires_at > local_time(),
        MemberSession.csrf_nonce.is_not(None),
    ))


def revoke_session(token, *, session: Session, reason="logout"):
    result = session.execute(update(MemberSession).where(
        MemberSession.token_hash == token_digest(token),
        MemberSession.revoked_at.is_(None),
    ).values(revoked_at=local_time(), revoke_reason=reason))
    return result.rowcount == 1


def revoke_all_sessions(member_id, except_token=None, reason="forced_logout", *, session: Session):
    statement = update(MemberSession).where(
        MemberSession.member_id == member_id, MemberSession.revoked_at.is_(None),
    )
    if except_token:
        statement = statement.where(MemberSession.token_hash != token_digest(except_token))
    session.execute(statement.values(revoked_at=local_time(), revoke_reason=reason))


def rotate_session(token, *, session: Session, ttl_hours=8, reason="rotation"):
    current = load_session(token, session=session)
    if current is None or not revoke_session(token, session=session, reason=reason):
        return None
    return create_session(
        current.member_id, current.user_agent, current.ip,
        session=session, ttl_hours=ttl_hours,
    )
