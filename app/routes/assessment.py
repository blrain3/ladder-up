# 测评、成绩录入、成员考核与转正。

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from bin.job import JobFailed, run_job

from .. import routes
from ..assessment import (
    AssessmentError,
    current_term,
    parse_kp_snapshot,
    promote,
    promotion_items,
    record_scores,
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
    AssessmentTest,
    JobRun,
    Member,
    MemberAssessment,
    TestScore,
)
from ..models.base import local_time
from ..web import page, success, wants_html
from ._common import (
    SHANGHAI,
    api_error,
    approve_member_scope,
    bank_write,
    enum_field,
    multi_ids_field,
    object_id,
    parse_day_field,
    payload,
    redirect,
    required_text,
    s5_page,
    s5_pagination,
    s5_read_scope,
    s5_target,
    training_member_scope,
)
from .members import MEMBER_APPROVER
from .training import TRAINING_APPROVER, TRAINING_READER

router = APIRouter()


ASSESSMENT_TEST_TYPES = ("周测", "月测")
ASSESSMENT_READER = require_permission("member:read", "member:read:self", target_type="assessment")
ASSESSMENT_RUNNER = require_permission("system:admin", target_type="assessment")


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
            raw_snapshot = data.get(f"kp_snapshot_{member_id}")
            snapshot_changed = raw_snapshot is not None and raw_snapshot != data.get(f"old_kp_snapshot_{member_id}", "")
            if not snapshot_changed and (raw_solved, raw_score) == (
                data.get(f"old_solved_{member_id}", ""), data.get(f"old_score_{member_id}", ""),
            ):
                continue
            row = {"member_id": member_id, "solved_count": raw_solved, "score": raw_score,
                   "version": data.get(f"version_{member_id}")}
            if snapshot_changed:
                row["kp_snapshot"] = raw_snapshot
            rows.append(row)
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
            "kp_snapshot": parse_kp_snapshot(score.kp_snapshot) if score else {},
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
    biz_date = routes.datetime.now(SHANGHAI).date().isoformat()
    try:
        result = run_job(session, "assess", biz_date, lambda s: routes.run_assess_term(s, term),
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
