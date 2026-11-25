# 入会申请、审核、面试与录取。
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import (
    JSONResponse,
)
from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from .. import routes
from ..auth import (
    active_member,
    allows,
    audit,
    csrf_form,
    csrf_matches,
    permission_codes,
    require_permission,
)
from ..db import get_session
from ..models import (
    Department,
    Member,
    MemberApplication,
)
from ..models.base import local_time
from ..security import (
    hash_password,
    token_digest,
)
from ..web import page, parse_object_id, success, wants_html
from ._common import invalid, object_id, payload, redirect

router = APIRouter()


STUDENT_NO_RE = re.compile(r"^[0-9]{8,20}$")
PHONE_RE = re.compile(r"^[0-9]{11}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
APPLICATION_STATUSES = ("待审核", "待面试", "已面试", "已通过", "已拒绝", "已撤回")
APPLICATION_APPROVER = require_permission("member:approve", target_type="application")
APPLICATION_READER = require_permission("member:approve", "member:read", target_type="application")


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
    admitted = routes.ensure_member_for_application(session, application, local_time(), dept_id, password_hash)
    application.member_id = admitted.id
    audit(request, session, "application.admit", actor_id=member.id, target_id=id,
          target_type="application", detail={"member_id": admitted.id, "must_change_password": 1,
                                             "one_time_password_issued": True})
    session.commit()
    return page(request, "admit_success.html", admitted=admitted, one_time_password=password)
