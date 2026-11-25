# 训练计划、阶段、任务、打卡、进度、预警与建议。
import re
import sqlite3
from calendar import monthrange
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from .. import routes
from ..advice import training_advice
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
    Checkin,
    CheckinProblem,
    Member,
    Problem,
    SysConfig,
    TaskAssignee,
    TaskProblem,
    TrainingPhase,
    TrainingPlan,
    TrainingTask,
)
from ..models.base import local_time
from ..training import (
    MEMBER_ASSIGNEE,
    TASK_ASSIGNEE_TYPES,
    active_members_query,
    alert_items,
    alert_query,
    assignee_ids,
    checkin_details,
    makeup_checkin,
    member_summaries,
    member_tasks_query,
    named_assignees,
    publish_task,
    task_progress,
    task_totals,
    visible_to_member,
)
from ..web import page, parse_object_id, success, wants_html
from ._common import (
    SHANGHAI,
    api_error,
    bank_write,
    count_rows,
    enum_field,
    int_field,
    multi_ids_field,
    non_negative_int_field,
    object_id,
    optional_text,
    parse_day_field,
    payload,
    redirect,
    required_text,
    training_member_scope,
    training_page,
    training_read_all,
    training_viewer,
)
from .oj import oj_target

router = APIRouter()
advice_router = APIRouter()


TRAINING_READER = require_permission("training:read", target_type="training")
TRAINING_WRITER = require_permission("training:write", target_type="training")
TRAINING_APPROVER = require_permission("training:approve", target_type="training")
CHECKIN_WRITER = require_permission("checkin:write:self", target_type="training")
PLAN_INTENSITIES = ("低", "中", "高")


def plan_interval(term):
    """学期区间含首尾。寒假为 1 月 1 日至 2 月末，暑假为 7 月 1 日至 8 月 31 日。"""
    match = re.fullmatch(r"([0-9]{4})-([0-9]{4})-([12])", term)
    if match:
        year1, year2 = int(match[1]), int(match[2])
        if 2000 <= year1 < year2 <= 2100 and year2 == year1 + 1:
            if match[3] == "1":
                return date(year1, 9, 1), date(year2, 1, 31)
            return date(year2, 2, 1), date(year2, 8, 31)
    match = re.fullmatch(r"([0-9]{4})-(winter|summer)", term)
    if match and 2000 <= (year := int(match[1])) <= 2100:
        if match[2] == "winter":
            return date(year, 1, 1), date(year, 2, monthrange(year, 2)[1])
        return date(year, 7, 1), date(year, 8, 31)
    raise api_error(400, 40001, "学期格式或年份无效")


def problem_ids_field(data):
    """题目多选：表单 getlist 保序，JSON 保序；返回去重后的题目 id 列表."""
    if isinstance(data, FormData):
        raw = data.getlist("problem_ids")
    else:
        raw = data.get("problem_ids")
    if raw in (None, ""):
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise api_error(400, 40001, "题目参数无效")
    parts = []
    for item in raw:
        for part in str(item).replace("，", ",").split(","):
            part = part.strip()
            if part:
                parts.append(part)
    ids = []
    for part in parts:
        try:
            ids.append(parse_object_id(part))
        except ValueError:
            raise api_error(400, 40001, "题目参数无效") from None
    return list(dict.fromkeys(ids))


def deadline_field(data, name="deadline_at"):
    raw = data.get(name)
    if raw in (None, ""):
        return None
    if not isinstance(raw, str):
        raise api_error(400, 40001, "参数校验失败")
    value = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = routes.datetime.strptime(value, fmt)
            if fmt == "%Y-%m-%d":
                parsed = parsed.replace(hour=23, minute=59, second=59)
            return parsed.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    raise api_error(400, 40001, "截止时间格式应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM") from None


def deadline_passed(task):
    if not task.deadline_at:
        return False
    try:
        deadline = routes.datetime.fromisoformat(deadline_field({"deadline_at": task.deadline_at}))
    except (ValueError, HTTPException):
        return False
    return routes.datetime.now(SHANGHAI).replace(tzinfo=None, microsecond=0) > deadline


def active_training_task(session, task_id):
    task = session.get(TrainingTask, task_id)
    if task is None:
        raise api_error(404, 40401, "训练任务不存在")
    return task


def phase_item(session, phase):
    plan = session.get(TrainingPlan, phase.plan_id)
    return {
        "id": phase.id, "plan_id": phase.plan_id,
        "plan_term": plan.term if plan else None, "name": phase.name,
        "start_on": phase.start_on, "end_on": phase.end_on,
    }


def task_assignee_names(session, task_id, *, only_member_id=None):
    statement = select(Member).join(TaskAssignee, TaskAssignee.assignee_id == Member.id).where(
        TaskAssignee.task_id == task_id, TaskAssignee.assignee_type == MEMBER_ASSIGNEE,
    )
    if only_member_id is not None:
        statement = statement.where(Member.id == only_member_id)
    return [
        {"type": "会员", "id": member.id, "name": member.name, "student_no": member.student_no}
        for member in session.scalars(statement.order_by(Member.id))
    ]


def task_problem_rows(session, task_id):
    rows = session.execute(
        select(TaskProblem, Problem)
        .join(Problem, Problem.id == TaskProblem.problem_id)
        .where(TaskProblem.task_id == task_id, Problem.deleted_at.is_(None))
        .order_by(TaskProblem.sort_no, TaskProblem.problem_id)
    ).all()
    return [
        {
            "problem_id": problem.id, "title": problem.title, "difficulty": problem.difficulty,
            "problem_type": problem.problem_type, "sort_no": link.sort_no,
        }
        for link, problem in rows
    ]


def task_list_items(session, tasks):
    totals = task_totals()
    assignees = select(TaskAssignee.task_id, func.count().label("total")).where(
        TaskAssignee.assignee_type == MEMBER_ASSIGNEE
    ).group_by(TaskAssignee.task_id).subquery()
    selected = tasks.subquery()
    statement = (
        select(selected, TrainingPhase.name.label("phase_name"), TrainingPlan.id.label("plan_id"),
               TrainingPlan.term.label("plan_term"), func.coalesce(assignees.c.total, 0).label("assignee_count"),
               func.coalesce(totals.c.total, 0).label("problem_count"))
        .join(TrainingPhase, TrainingPhase.id == selected.c.phase_id)
        .join(TrainingPlan, TrainingPlan.id == TrainingPhase.plan_id)
        .outerjoin(assignees, assignees.c.task_id == selected.c.id)
        .outerjoin(totals, totals.c.task_id == selected.c.id)
        .order_by(selected.c.id.desc())
    )
    return [dict(row) for row in session.execute(statement).mappings()]


def task_detail_payload(session, task, *, member_id, read_all):
    data = task_list_items(session, select(TrainingTask).where(TrainingTask.id == task.id))[0]
    data["problems"] = task_problem_rows(session, task.id)
    data["assignees"] = task_assignee_names(session, task.id, only_member_id=None if read_all else member_id)
    data["progress"] = task_progress(session, task.id, member_id)
    return data


def threshold_from_config(session):
    raw = session.scalar(select(SysConfig.config_value).where(
        SysConfig.config_key == "training.alert_threshold"
    ))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.6
    return value if 0 <= value <= 1 else 0.6


@router.get("/training-plans")
def training_plans(request: Request, member: Member = Depends(TRAINING_READER),
                   session: Session = Depends(get_session)):
    plans = session.scalars(
        select(TrainingPlan).order_by(TrainingPlan.id.desc())
    ).all()
    counts = dict(session.execute(
        select(TrainingPhase.plan_id, func.count())
        .group_by(TrainingPhase.plan_id)
    ).all()) if plans else {}
    data = {
        "total": len(plans),
        "items": [
            {
                "id": plan.id, "term": plan.term, "goal": plan.goal,
                "intensity": plan.intensity, "created_at": plan.created_at,
                "phase_count": counts.get(plan.id, 0),
            }
            for plan in plans
        ],
    }
    if wants_html(request):
        return page(request, "training_plans.html", plans=data, intensities=PLAN_INTENSITIES)
    return success(data)


@router.post("/training-plans")
def create_training_plan(request: Request, data=Depends(payload),
                         member: Member = Depends(TRAINING_WRITER),
                         session: Session = Depends(get_session)):
    with bank_write(session, "该学期的训练计划已存在"):
        term = required_text(data, "term", "学期", 20)
        plan_interval(term)
        existing = session.scalar(select(TrainingPlan.id).where(TrainingPlan.term == term))
        if existing is not None:
            raise api_error(409, 40901, "该学期的训练计划已存在", {"id": existing})
        goal = optional_text(data, "goal", 500)
        intensity = None
        if data.get("intensity") not in (None, ""):
            intensity = enum_field(data, "intensity", PLAN_INTENSITIES, "强度")
        plan = TrainingPlan(term=term, goal=goal, intensity=intensity, created_at=local_time())
        session.add(plan)
        session.flush()
        audit(request, session, "training.plan.created", actor_id=member.id, target_id=plan.id,
              target_type="training_plan", detail={"term": term})
    if wants_html(request):
        return redirect("/training-plans")
    return success({"id": plan.id, "term": plan.term})


@router.get("/training-phases")
def training_phases(request: Request, plan_id: int | None = Query(None),
                    member: Member = Depends(TRAINING_READER),
                    session: Session = Depends(get_session)):
    statement = select(TrainingPhase)
    if plan_id is not None:
        statement = statement.where(TrainingPhase.plan_id == plan_id)
    phases = session.scalars(statement.order_by(TrainingPhase.start_on, TrainingPhase.id)).all()
    data = {"total": len(phases), "items": [phase_item(session, phase) for phase in phases]}
    if wants_html(request):
        plans = session.scalars(select(TrainingPlan).order_by(TrainingPlan.id.desc())).all()
        return page(request, "training_phases.html", phases=data, plans=plans,
                    selected_plan_id=plan_id)
    return success(data)


@router.post("/training-phases")
def create_training_phase(request: Request, data=Depends(payload),
                          member: Member = Depends(TRAINING_WRITER),
                          session: Session = Depends(get_session)):
    with bank_write(session, "训练阶段冲突"):
        plan_id = int_field(data, "plan_id", required=True)
        plan = session.get(TrainingPlan, plan_id)
        if plan is None:
            raise api_error(400, 40002, "训练计划不存在")
        name = required_text(data, "name", "阶段名称", 40)
        start_on = parse_day_field(data, "start_on", "开始日期")
        end_on = parse_day_field(data, "end_on", "结束日期")
        if start_on > end_on:
            raise api_error(400, 40001, "阶段结束日期不能早于开始日期")
        interval_start, interval_end = plan_interval(plan.term)
        if start_on < interval_start or end_on > interval_end:
            raise api_error(400, 40002, "阶段日期必须在计划区间内")
        # 阶段为闭区间，同一天不能归属同一计划的两个阶段。
        overlap = session.scalar(select(TrainingPhase.id).where(
            TrainingPhase.plan_id == plan_id,
            TrainingPhase.start_on <= end_on.isoformat(),
            TrainingPhase.end_on >= start_on.isoformat(),
        ))
        if overlap is not None:
            raise api_error(400, 40002, "训练阶段日期与已有阶段重叠", {"id": overlap})
        phase = TrainingPhase(
            plan_id=plan_id, name=name, start_on=start_on.isoformat(), end_on=end_on.isoformat(),
        )
        session.add(phase)
        session.flush()
        audit(request, session, "training.phase.created", actor_id=member.id, target_id=phase.id,
              target_type="training_phase",
              detail={"plan_id": plan_id, "name": name, "start_on": phase.start_on,
                      "end_on": phase.end_on})
    if wants_html(request):
        return redirect("/training-phases")
    return success({"id": phase.id, "name": phase.name})


@router.get("/training-tasks")
def training_tasks(request: Request, phase_id: int | None = Query(None),
                   deadline_from: str | None = Query(None), deadline_to: str | None = Query(None),
                   page_number: int = Query(1, ge=1, alias="page"), page_size: int = Query(50, ge=1, le=100),
                   member: Member = Depends(training_viewer),
                   session: Session = Depends(get_session)):
    codes = permission_codes(member, session)
    read_all = allows(codes, "training:read")
    can_write = allows(codes, "training:write")
    statement: Select[TrainingTask] = select(TrainingTask) if read_all else member_tasks_query(member.id)
    if phase_id is not None:
        statement = statement.where(TrainingTask.phase_id == phase_id)
    for raw, lower in ((deadline_from, True), (deadline_to, False)):
        bound = parse_day_field({"date": raw}, "date", "截止日期", required=False)
        if bound:
            day = func.date(TrainingTask.deadline_at)
            statement = statement.where(day >= bound.isoformat() if lower else day <= bound.isoformat())
    total = count_rows(session, statement)
    tasks = statement.order_by(TrainingTask.id.desc()).offset((page_number - 1) * page_size).limit(page_size)
    data = {"total": total, "items": task_list_items(session, tasks), "read_all": read_all}
    if wants_html(request):
        phases = session.scalars(select(TrainingPhase).order_by(TrainingPhase.start_on)).all()
        problems = session.scalars(
            select(Problem).where(Problem.deleted_at.is_(None)).order_by(Problem.id)
        ).all() if can_write else []
        members = session.scalars(active_members_query().order_by(Member.student_no)).all() if can_write else []
        return page(request, "training_tasks.html", tasks=data, phases=phases, can_write=can_write,
                    selected_phase_id=phase_id, assignee_types=TASK_ASSIGNEE_TYPES, members=members,
                    problems=problems, pager=training_page(request, page_number, page_size, page_number * page_size < total))
    return success(data)


def selected_assignees(data, session):
    try:
        return named_assignees(session, multi_ids_field(data, "member_ids"))
    except ValueError as exc:
        raise api_error(400, 40002, str(exc)) from None


@router.post("/training-tasks")
def create_training_task(request: Request, data=Depends(payload),
                         member: Member = Depends(TRAINING_WRITER),
                         session: Session = Depends(get_session)):
    with bank_write(session, "训练任务创建冲突"):
        phase_id = int_field(data, "phase_id", required=True)
        if session.get(TrainingPhase, phase_id) is None:
            raise api_error(400, 40002, "训练阶段不存在")
        title = required_text(data, "title", "任务标题", 200)
        deadline_at = deadline_field(data)
        assignee_type = enum_field(data, "assignee_type", TASK_ASSIGNEE_TYPES, "指派类型",
                                   default="指定会员")
        problem_ids = problem_ids_field(data)
        if not problem_ids:
            raise api_error(400, 40001, "请选择至少一道题目")
        task = TrainingTask(
            phase_id=phase_id, title=title, assignee_type=assignee_type,
            deadline_at=deadline_at, status="草稿", created_by=member.id,
            created_at=local_time(),
        )
        session.add(task)
        session.flush()
        if assignee_type == "指定会员":
            for kind, member_id in selected_assignees(data, session):
                session.add(TaskAssignee(task_id=task.id, assignee_type=kind, assignee_id=member_id))
        for sort_no, problem_id in enumerate(problem_ids, start=1):
            problem = session.get(Problem, problem_id)
            if problem is None or problem.deleted_at is not None:
                raise api_error(400, 40002, "题目包中存在无效题目")
            session.add(TaskProblem(task_id=task.id, problem_id=problem_id, sort_no=sort_no))
        audit(request, session, "training.task.created", actor_id=member.id, target_id=task.id,
              target_type="training_task", detail={"title": title, "problem_ids": problem_ids})
    if wants_html(request):
        return redirect(f"/training-tasks/{task.id}")
    return success({"id": task.id, "title": task.title})


@router.get("/training-tasks/{id}")
def training_task_detail(request: Request, id: int = Depends(object_id),
                         member: Member = Depends(training_viewer),
                         session: Session = Depends(get_session)):
    task = active_training_task(session, id)
    codes = permission_codes(member, session)
    read_all = allows(codes, "training:read")
    if not read_all and not visible_to_member(session, task.id, member.id):
        audit(request, session, "auth.permission.denied", actor_id=member.id,
              target_id=task.id, target_type="training")
        session.commit()
        raise api_error(404, 40401, "训练任务不存在")
    data = task_detail_payload(session, task, member_id=member.id, read_all=read_all)
    data["read_all"] = read_all
    if wants_html(request):
        can_assign = task.status == "草稿" and allows(codes, "training:write")
        members = session.scalars(active_members_query().order_by(Member.student_no)).all() if can_assign else []
        return page(request, "task_detail.html", task=data, members=members, can_assign=can_assign,
                    assignee_types=TASK_ASSIGNEE_TYPES,
                    can_checkin=allows(codes, "checkin:write:self") and visible_to_member(session, task.id, member.id))
    return success(data)


@router.patch("/training-tasks/{id}")
def patch_training_task(request: Request, id: int = Depends(object_id), data=Depends(payload),
                        member: Member = Depends(TRAINING_WRITER),
                        session: Session = Depends(get_session)):
    with bank_write(session, "训练任务修改冲突", legacy_busy_response=False):
        task = active_training_task(session, id)
        if set(data) - {"title", "deadline_at", "csrf_token"}:
            raise api_error(400, 40001, "只能修改标题和截止时间")
        if "title" in data:
            task.title = required_text(data, "title", "任务标题", 200)
        if "deadline_at" in data:
            task.deadline_at = deadline_field(data)
        audit(request, session, "training.task.updated", actor_id=member.id, target_id=task.id,
              target_type="training_task", detail={"title": task.title, "deadline_at": task.deadline_at})
    return success({"id": task.id, "title": task.title, "deadline_at": task.deadline_at})


@router.post("/training-tasks/{id}/publish", include_in_schema=False)
@router.post("/training-tasks/{id}/assign")
def assign_training_task(request: Request, id: int = Depends(object_id), data=Depends(payload),
                         member: Member = Depends(TRAINING_WRITER),
                         session: Session = Depends(get_session)):
    with bank_write(session, "训练任务指派冲突", legacy_busy_response=False):
        task = active_training_task(session, id)
        assignee_type = enum_field(data, "assignee_type", TASK_ASSIGNEE_TYPES, "指派类型",
                                   default=task.assignee_type)
        if task.status != "草稿":
            raise api_error(400, 40002, "任务已下发，不能修改指派")
        rows: list[tuple[str, int]] = []
        if assignee_type == "指定会员":
            rows = selected_assignees(data, session) if "member_ids" in data else sorted(
                row for row in assignee_ids(session, id) if row[0] == MEMBER_ASSIGNEE
            )
            if not rows:
                raise api_error(400, 40002, "请选择指派对象")
        task.assignee_type = assignee_type
        written = publish_task(session, task, rows)
        audit(request, session, "training.task.published", actor_id=member.id, target_id=task.id,
              target_type="training_task", detail={"assignee_type": assignee_type, "assignees": written})
    if wants_html(request):
        return redirect(f"/training-tasks/{task.id}")
    return success({"id": task.id, "status": task.status, "assignee_type": assignee_type, "assignees": written})


def checkin_items_payload(session, checkins):
    items = []
    details = checkin_details(session, [checkin.id for checkin in checkins])
    for checkin in checkins:
        problems = details.get(checkin.id, [])
        solved = sum(row["solved"] for row in problems)
        items.append({
            "id": checkin.id, "task_id": checkin.task_id, "member_id": checkin.member_id,
            "checkin_date": checkin.checkin_date, "minutes": checkin.minutes,
            "stored_solved_count": checkin.solved_count, "solved_count": solved,
            "status": checkin.status, "source": checkin.source, "note": checkin.note,
            "problems": problems,
            "inconsistent": checkin.solved_count != solved,
        })
    return items


@router.get("/checkins")
def checkins_page(request: Request, task_id: int | None = Query(None), member_id: int | None = Query(None),
                  checkin_date: str | None = Query(None), date_from: str | None = Query(None),
                  date_to: str | None = Query(None),
                  page_number: int = Query(1, ge=1, alias="page"), page_size: int = Query(50, ge=1, le=100),
                  member: Member = Depends(training_viewer),
                  session: Session = Depends(get_session)):
    read_all = training_read_all(member, session)
    statement = select(Checkin)
    if read_all:
        tasks = select(TrainingTask).where(TrainingTask.published_at.isnot(None))
        if member.role == "dept_admin":
            statement = statement.where(Checkin.member_id.in_(training_member_scope(member).with_only_columns(Member.id)))
    else:
        tasks = member_tasks_query(member.id)
        statement = statement.where(Checkin.member_id == member.id)
    if task_id is not None:
        statement = statement.where(Checkin.task_id == task_id)
    if member_id is not None:
        statement = statement.where(Checkin.member_id == member_id)
    dates = {"checkin_date": checkin_date, "date_from": date_from, "date_to": date_to}
    exact, start, end = (
        parse_day_field(dates, key, "打卡日期", required=False) for key in dates
    )
    if exact:
        statement = statement.where(Checkin.checkin_date == exact.isoformat())
    if start:
        statement = statement.where(Checkin.checkin_date >= start.isoformat())
    if end:
        statement = statement.where(Checkin.checkin_date <= end.isoformat())
    selected_task = None
    if task_id is not None:
        selected_task = active_training_task(session, task_id)
        if not read_all and not visible_to_member(session, selected_task.id, member.id):
            deny(request, session, selected_task.id, "training")
    total = count_rows(session, statement)
    checkins = session.scalars(statement.order_by(Checkin.checkin_date.desc(), Checkin.id.desc())
                               .offset((page_number - 1) * page_size).limit(page_size)).all()
    data = {
        "total": total,
        "items": checkin_items_payload(session, checkins),
        "tasks": task_list_items(session, tasks),
        "read_all": read_all,
    }
    if wants_html(request):
        can_checkin = selected_task and allows(permission_codes(member, session), "checkin:write:self") \
            and visible_to_member(session, selected_task.id, member.id)
        selected_problems = task_problem_rows(session, selected_task.id) if can_checkin and selected_task is not None else []
        return page(
            request, "checkin.html", checkins=data, selected_task_id=task_id,
            selected_problems=selected_problems,
            pager=training_page(request, page_number, page_size, page_number * page_size < total),
            today=routes.datetime.now(SHANGHAI).date().isoformat(),
        )
    return success(data)


def parse_checkin_problems(data, task, session):
    task_problem_ids = [row["problem_id"] for row in task_problem_rows(session, task.id)]
    if isinstance(data, FormData):
        checked = set(multi_ids_field(data, "solved_problem_ids"))
        if checked.difference(task_problem_ids):
            raise api_error(400, 40001, "题目不在任务有效题包中")
        return [
            {"problem_id": problem_id, "solved": 1 if problem_id in checked else 0, "attempts": None}
            for problem_id in task_problem_ids
        ]
    raw = data.get("problems")
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise api_error(400, 40001, "逐题完成情况格式无效")
    rows: dict[int, dict[str, int | None]] = {}
    active_ids = set(task_problem_ids)
    for item in raw:
        if not isinstance(item, dict):
            raise api_error(400, 40001, "逐题完成情况格式无效")
        try:
            problem_id = parse_object_id(str(item.get("problem_id", "")).strip())
        except ValueError:
            raise api_error(400, 40001, "逐题完成情况格式无效") from None
        if problem_id not in active_ids:
            raise api_error(400, 40001, "题目不在任务有效题包中")
        solved_value = item.get("solved", 0)
        if solved_value not in (0, 1, "0", "1"):
            raise api_error(400, 40001, "逐题完成情况格式无效")
        attempts = item.get("attempts")
        if attempts in ("", None):
            attempts = None
        else:
            try:
                attempts = int(str(attempts))
            except (TypeError, ValueError):
                raise api_error(400, 40001, "尝试次数无效") from None
        row = {"problem_id": problem_id, "solved": int(solved_value), "attempts": attempts}
        # 同题以已解记录优先，每题只保存一次。
        if problem_id not in rows or (row["solved"] and not rows[problem_id]["solved"]):
            rows[problem_id] = row
    return list(rows.values())


@router.post("/checkins")
def create_checkin(request: Request, data=Depends(payload),
                   member: Member = Depends(CHECKIN_WRITER),
                   session: Session = Depends(get_session)):
    with bank_write(session, "打卡保存冲突", legacy_busy_response=False):
        task_id = int_field(data, "task_id", required=True)
        task = active_training_task(session, task_id)
        if "member_id" in data and int_field(data, "member_id", required=True) != member.id:
            deny(request, session, task.id, "training")
        if not visible_to_member(session, task.id, member.id):
            deny(request, session, task.id, "training")
        day = checkin_day(data, required=False)
        if deadline_passed(task):
            raise api_error(400, 40002, "该任务已截止，请联系教练补卡")
        minutes = non_negative_int_field(data, "minutes")
        solved_count = non_negative_int_field(data, "solved_count", required=True)
        problems = parse_checkin_problems(data, task, session)
        detail_solved = sum(row["solved"] for row in problems)
        if detail_solved != solved_count:
            raise api_error(400, 40001, "已解题数与逐题明细不一致")
        checkin = Checkin(
            task_id=task.id, member_id=member.id, checkin_date=day, minutes=minutes,
            solved_count=solved_count, status="打卡", source="手工", created_at=local_time(),
        )
        session.add(checkin)
        try:
            session.flush()
        except IntegrityError as exc:
            if (getattr(exc.orig, "sqlite_errorcode", None) != sqlite3.SQLITE_CONSTRAINT_UNIQUE
                    or str(exc.orig) != "UNIQUE constraint failed: checkin.member_id, checkin.task_id, checkin.checkin_date"):
                raise
            session.rollback()
            existing_id = session.scalar(select(Checkin.id).where(
                Checkin.member_id == member.id, Checkin.task_id == task_id, Checkin.checkin_date == day,
            ))
            raise api_error(409, 40901, "今日已打卡，不能重复提交", {"existingCheckinId": existing_id}) from None
        for row in problems:
            session.add(CheckinProblem(
                checkin_id=checkin.id, problem_id=row["problem_id"],
                solved=row["solved"], attempts=row["attempts"],
            ))
        audit(request, session, "checkin.created", actor_id=member.id, target_id=checkin.id,
              target_type="checkin", detail={"task_id": task.id, "checkin_date": day,
                                             "solved_count": solved_count})
    if wants_html(request):
        return redirect(f"/training-tasks/{task.id}")
    return success({"id": checkin.id, "solved_count": solved_count})


def checkin_day(data, *, required):
    today = routes.datetime.now(SHANGHAI).date()
    day = parse_day_field(data, "checkin_date", "打卡日期", required=required) or today
    if day > today:
        raise api_error(400, 40001, "打卡日期不能晚于今天")
    return day.isoformat()


@router.post("/checkins/makeup")
def makeup_checkin_route(request: Request, data=Depends(payload),
                         member: Member = Depends(TRAINING_APPROVER),
                         session: Session = Depends(get_session)):
    with bank_write(session, "补卡保存冲突", legacy_busy_response=False):
        task_id = int_field(data, "task_id", required=True)
        member_id = int_field(data, "member_id", required=True)
        task = active_training_task(session, task_id)
        target = session.get(Member, member_id)
        if target is None or target.deleted_at is not None or target.member_status in {"已退会", "已除名"}:
            raise api_error(400, 40002, "补卡会员不存在或已离会")
        if not visible_to_member(session, task.id, member_id):
            raise api_error(400, 40002, "任务尚未下发或未指派给该会员")
        day = checkin_day(data, required=True)
        note = optional_text(data, "note", 500)
        row, created = makeup_checkin(session, member_id, task_id, day, note, member.id)
        audit(request, session, "checkin.makeup", actor_id=member.id, target_id=row.id,
              target_type="checkin", detail={"member_id": member_id, "task_id": task_id,
                                             "checkin_date": day, "created": created, "note": note})
    if wants_html(request):
        return redirect("/checkins")
    return success({"id": row.id, "created": created, "status": row.status})


@router.get("/training-progress")
def training_progress_page(request: Request, member_id: int | None = Query(None),
                           phase_id: int | None = Query(None),
                           page_number: int = Query(1, ge=1, alias="page"), page_size: int = Query(50, ge=1, le=100),
                           member: Member = Depends(training_viewer),
                           session: Session = Depends(get_session)):
    read_all = training_read_all(member, session)
    view_all = read_all and member_id is None
    offset = (page_number - 1) * page_size
    if view_all:
        scope = training_member_scope(member).with_only_columns(Member.id)
        member_ids = list(session.scalars(scope.order_by(Member.id).offset(offset).limit(page_size + 1)))
        has_next = len(member_ids) > page_size
        items = member_summaries(session, member_ids[:page_size], phase_id)
    else:
        target_id = member_id if member_id is not None else member.id
        if (not read_all and target_id != member.id) or (
            member.role == "dept_admin" and session.scalar(
                training_member_scope(member).with_only_columns(Member.id).where(Member.id == target_id)
            ) is None
        ):
            deny(request, session, target_id, "training")
        tasks = member_tasks_query(target_id)
        if phase_id is not None:
            tasks = tasks.where(TrainingTask.phase_id == phase_id)
        has_next = page_number * page_size < count_rows(session, tasks)
        items = member_summaries(session, [target_id], phase_id, task_offset=offset, task_limit=page_size)
    data = {
        "items": items, "view_all": view_all, "read_all": read_all, "phase_id": phase_id,
        "total": sum(item["total"] for item in items), "solved": sum(item["solved"] for item in items),
        "has_next": has_next,
    }
    if wants_html(request):
        phases = session.scalars(select(TrainingPhase).order_by(TrainingPhase.start_on)).all()
        return page(request, "training_progress.html", progress=data, phases=phases,
                    selected_phase_id=phase_id, pager=training_page(request, page_number, page_size, has_next))
    return success(data)


@router.get("/training-alerts")
def training_alerts(request: Request, phase_id: int | None = Query(None),
                    page_number: int = Query(1, ge=1, alias="page"), page_size: int = Query(50, ge=1, le=100),
                    member: Member = Depends(TRAINING_READER),
                    session: Session = Depends(get_session)):
    threshold = threshold_from_config(session)
    scope = training_member_scope(member).with_only_columns(Member.id)
    statement = alert_query(phase_id, threshold, member_ids=scope)
    total = count_rows(session, statement)
    items = alert_items(session, statement.offset((page_number - 1) * page_size).limit(page_size))
    has_next = page_number * page_size < total
    data = {"threshold": threshold, "total": total, "items": items, "has_next": has_next}
    if wants_html(request):
        phases = session.scalars(select(TrainingPhase).order_by(TrainingPhase.start_on)).all()
        return page(request, "training_alerts.html", alerts=data, phases=phases,
                    selected_phase_id=phase_id, pager=training_page(request, page_number, page_size, has_next))
    return success(data)


@advice_router.get("/training-advice")
def training_advice_page(request: Request, member_id: int | None = Query(None, ge=1, le=2**63-1),
                         actor: Member = Depends(authenticated), session: Session = Depends(get_session)):
    target = oj_target(request, session, actor, member_id)
    items = training_advice(session, target.id)
    if wants_html(request):
        return page(request, "training_advice.html", target=target, advice=items)
    return success(items)
