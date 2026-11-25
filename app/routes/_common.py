# 共享请求解析、校验、分页与业务域通用查询。
import sqlite3
from contextlib import contextmanager
from datetime import date
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, Path, Query, Request
from fastapi.responses import (
    RedirectResponse,
)
from sqlalchemy import false, func, select, text, true
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from ..auth import (
    allows,
    authenticated,
    csrf_form,
    deny,
    permission_codes,
)
from ..db import get_session
from ..models import (
    Member,
)
from ..training import (
    active_members_query,
)
from ..web import content_type, parse_object_id

SHANGHAI = ZoneInfo("Asia/Shanghai")


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
