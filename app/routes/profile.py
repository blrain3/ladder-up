# 会员档案、CSV 导出、奖惩与统计。

from collections.abc import Sequence

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import (
    Response,
)
from sqlalchemy import ColumnElement, select, true
from sqlalchemy.orm import Session

from .. import routes
from ..assessment import (
    LEFT_STATUSES,
    term_interval,
)
from ..auth import (
    allows,
    audit,
    authenticated,
    deny,
    permission_codes,
    require_permission,
)
from ..db import get_session
from ..models import (
    Department,
    Member,
    MemberAward,
)
from ..models.base import local_time
from ..profile import (
    assessment_history,
    award_fields,
    award_summary,
    awards_of,
    can_view_sensitive,
    csv_filename_for,
    format_points,
    mask_contact,
    member_stats,
    profile_csv_sections,
    render_csv_sections,
    training_summary,
)
from ..training import (
    active_members_query,
)
from ..web import page, success, wants_html
from ._common import (
    SHANGHAI,
    api_error,
    approve_member_scope,
    bank_write,
    count_rows,
    enum_field,
    int_field,
    object_id,
    optional_text,
    parse_day_field,
    payload,
    redirect,
    required_text,
    training_page,
)
from .members import member_scope

router = APIRouter()


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
        member_options: Sequence[Member] = []
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
        today = routes.datetime.now(SHANGHAI).date()
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
    department_scope: ColumnElement[bool] = true()
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
