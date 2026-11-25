"""S4 训练纯逻辑：下发名单展开、进度复算、预警与补卡.

这些函数只做查询与对象组装，不依赖 FastAPI 请求上下文，便于单独测试。
日期统一使用 Asia/Shanghai 本地时间；字符串日期格式为 YYYY-MM-DD。
"""

from datetime import date, datetime
from typing import TypedDict, cast
from zoneinfo import ZoneInfo

from sqlalchemy import Table, func, select

from .models import (
    Checkin,
    CheckinProblem,
    Member,
    Problem,
    TaskAssignee,
    TaskProblem,
    TrainingPhase,
    TrainingTask,
)
from .models.base import local_time
from .models.training import TASK_ASSIGNEE_TYPES

SHANGHAI = ZoneInfo("Asia/Shanghai")

MEMBER_ASSIGNEE = "会员"
ALL_MEMBERS, NAMED_MEMBERS = TASK_ASSIGNEE_TYPES


def active_members_query():
    return select(Member).where(
        Member.deleted_at.is_(None),
        Member.member_status.notin_(["已退会", "已除名"]),
    )


def _active_members(session):
    return session.scalars(active_members_query().order_by(Member.id)).all()


def named_assignees(session, member_ids):
    """校验指定会员名单。队伍指派随 E 域接入。"""
    valid = set(session.scalars(active_members_query().with_only_columns(Member.id).where(
        Member.id.in_(member_ids)
    )))
    if valid != set(member_ids):
        raise ValueError("指定的会员不存在或已离会")
    return [(MEMBER_ASSIGNEE, member_id) for member_id in member_ids]


def assignee_ids(session, task_id: int) -> set[tuple[str, int]]:
    return set(session.execute(
        select(TaskAssignee.assignee_type, TaskAssignee.assignee_id).where(
            TaskAssignee.task_id == task_id
        )
    ).all())


def visible_to_member(session, task_id: int, member_id: int) -> bool:
    task = session.get(TrainingTask, task_id)
    if task is None or task.published_at is None or task.status not in {"已下发", "已结束"}:
        return False
    return session.get(TaskAssignee, (task_id, MEMBER_ASSIGNEE, member_id)) is not None


def publish_task(session, task, rows) -> int:
    """冻结名单并下发，由调用方提交事务。"""
    if task.assignee_type == ALL_MEMBERS:
        rows = [(MEMBER_ASSIGNEE, member.id) for member in _active_members(session)]
    session.execute(
        cast(Table, TaskAssignee.__table__).delete().where(TaskAssignee.task_id == task.id)
    )
    session.flush()
    for assignee_type, assignee_id in rows:
        session.add(TaskAssignee(task_id=task.id, assignee_type=assignee_type, assignee_id=assignee_id))
    task.published_at = local_time()
    task.status = "已下发"
    session.flush()
    return len(rows)


def task_problem_total(session, task_id: int) -> int:
    """任务题目总数，软删题目不计入分母."""
    return session.scalar(
        select(func.count())
        .select_from(TaskProblem)
        .join(Problem, Problem.id == TaskProblem.problem_id)
        .where(TaskProblem.task_id == task_id, Problem.deleted_at.is_(None))
    ) or 0


def checkin_details(session, checkin_ids):
    """批量读取历史逐题明细，包括已软删题目。"""
    details: dict[int, list[dict[str, int | str | None]]] = {}
    if checkin_ids:
        rows = session.execute(
            select(CheckinProblem, Problem.title).join(Problem)
            .where(CheckinProblem.checkin_id.in_(checkin_ids))
            .order_by(CheckinProblem.problem_id)
        )
        for detail, title in rows:
            details.setdefault(detail.checkin_id, []).append({
                "problem_id": detail.problem_id, "title": title, "solved": detail.solved,
                "attempts": detail.attempts,
            })
    return details


def task_progress(session, task_id: int, member_id: int) -> dict:
    """进度只从 checkin_problem 派生；solved_count 仅作展示并校验一致性."""
    total = task_problem_total(session, task_id)
    checkins = session.scalars(
        select(Checkin).where(
            Checkin.task_id == task_id, Checkin.member_id == member_id,
        ).order_by(Checkin.checkin_date.desc(), Checkin.id.desc())
    ).all()
    active_ids = set(session.scalars(select(TaskProblem.problem_id).join(Problem).where(
        TaskProblem.task_id == task_id, Problem.deleted_at.is_(None),
    )))
    solved_ids: set[int] = set()
    details = checkin_details(session, [row.id for row in checkins])
    checkin_items = []
    for checkin in checkins:
        problems = details.get(checkin.id, [])
        detail_solved = sum(row["solved"] for row in problems)
        solved_ids.update(row["problem_id"] for row in problems if row["solved"])
        checkin_items.append({
            "id": checkin.id, "checkin_date": checkin.checkin_date,
            "minutes": checkin.minutes, "stored_solved_count": checkin.solved_count,
            "solved_count": detail_solved, "status": checkin.status,
            "note": checkin.note, "source": checkin.source,
            "problems": problems,
            "inconsistent": checkin.solved_count != detail_solved,
        })
    last_date = checkins[0].checkin_date if checkins else None
    solved = len(solved_ids & active_ids)
    rate = (solved / total) if total else 0.0
    return {
        "task_id": task_id, "member_id": member_id, "total": total, "solved": solved,
        "rate": round(rate, 4), "checkins": checkin_items, "last_date": last_date,
    }


def member_tasks_query(member_id: int):
    return select(TrainingTask).join(TaskAssignee).where(
        *published_tasks(),
        TaskAssignee.assignee_type == MEMBER_ASSIGNEE,
        TaskAssignee.assignee_id == member_id,
    )


def published_tasks():
    return (
        TrainingTask.published_at.isnot(None),
        TrainingTask.status.in_(["已下发", "已结束"]),
    )


def task_totals():
    return (
        select(TaskProblem.task_id, func.count().label("total")).join(Problem)
        .where(Problem.deleted_at.is_(None)).group_by(TaskProblem.task_id).subquery()
    )


def progress_query(phase_id, *, member_ids=None, today=None):
    """按冻结指派聚合。解题数按当前有效题包内的不同题目计算。"""
    statement = select(TrainingTask.id.label("task_id"), TaskAssignee.assignee_id.label("member_id")).join(
        TaskAssignee
    ).where(*published_tasks(), TaskAssignee.assignee_type == MEMBER_ASSIGNEE)
    if phase_id is not None:
        statement = statement.where(TrainingTask.phase_id == phase_id)
    if member_ids is not None:
        statement = statement.where(TaskAssignee.assignee_id.in_(member_ids))
    pairs = statement.cte("training_pairs")
    solved = (
        select(Checkin.task_id, Checkin.member_id, func.count(func.distinct(CheckinProblem.problem_id)).label("solved"))
        .join(pairs, (pairs.c.task_id == Checkin.task_id) & (pairs.c.member_id == Checkin.member_id))
        .join(CheckinProblem)
        .join(TaskProblem, (TaskProblem.task_id == Checkin.task_id)
              & (TaskProblem.problem_id == CheckinProblem.problem_id))
        .join(Problem, Problem.id == TaskProblem.problem_id)
        .where(CheckinProblem.solved == 1, Problem.deleted_at.is_(None))
        .group_by(Checkin.task_id, Checkin.member_id).subquery()
    )
    totals = task_totals()
    start = func.date(TrainingTask.published_at)
    end = func.min(func.coalesce(func.date(TrainingTask.deadline_at), today), today)
    records = (
        select(Checkin.task_id, Checkin.member_id, func.count().label("checkin_count"),
               func.max(Checkin.checkin_date).label("last_date"))
        .join(pairs, (pairs.c.task_id == Checkin.task_id) & (pairs.c.member_id == Checkin.member_id))
    )
    if today is not None:
        records = records.join(TrainingTask).where(Checkin.checkin_date.between(start, end))
    records_by_member = records.group_by(Checkin.task_id, Checkin.member_id).subquery()
    return (
        select(TrainingTask, pairs.c.member_id, func.coalesce(totals.c.total, 0).label("total"),
               func.coalesce(solved.c.solved, 0).label("solved"),
               func.coalesce(records_by_member.c.checkin_count, 0).label("checkin_count"), records_by_member.c.last_date)
        .select_from(pairs).join(TrainingTask, TrainingTask.id == pairs.c.task_id)
        .outerjoin(totals, totals.c.task_id == pairs.c.task_id)
        .outerjoin(solved, (solved.c.task_id == pairs.c.task_id) & (solved.c.member_id == pairs.c.member_id))
        .outerjoin(records_by_member, (records_by_member.c.task_id == pairs.c.task_id)
                   & (records_by_member.c.member_id == pairs.c.member_id))
    )


class _TaskSummary(TypedDict):
    id: int
    phase_id: int
    phase_name: str
    title: str
    assignee_type: str
    deadline_at: str | None
    status: str
    published_at: str | None
    total: int
    solved: int
    rate: float
    last_date: str | None
    checkin_count: int


def member_summaries(session, member_ids, phase_id, *, task_offset=0, task_limit=None):
    items: dict[int, list[_TaskSummary]] = {member_id: [] for member_id in member_ids}
    statement = progress_query(phase_id, member_ids=member_ids).add_columns(TrainingPhase.name).join(
        TrainingPhase, TrainingPhase.id == TrainingTask.phase_id
    ).order_by(TrainingTask.id).offset(task_offset).limit(task_limit)
    for task, member_id, total, solved, count, last_date, phase_name in session.execute(statement):
        items[member_id].append({
            "id": task.id, "phase_id": task.phase_id, "phase_name": phase_name,
            "title": task.title, "assignee_type": task.assignee_type,
            "deadline_at": task.deadline_at, "status": task.status, "published_at": task.published_at,
            "total": total, "solved": solved, "rate": round(solved / total, 4) if total else 0.0,
            "last_date": last_date, "checkin_count": count,
        })
    results = []
    for member_id, tasks in items.items():
        total = sum(task["total"] for task in tasks)
        solved = sum(task["solved"] for task in tasks)
        results.append({
            "member_id": member_id, "phase_id": phase_id, "task_count": len(tasks),
            "total": total, "solved": solved, "rate": round(solved / total, 4) if total else 0.0,
            "tasks": tasks,
        })
    return results


def alert_query(phase_id, threshold, *, today: date | None = None, missing_min=2, member_ids=None):
    """下发日到上海当天（或截止日）首尾均计，周末照计。"""
    day = (today or datetime.now(SHANGHAI).date()).isoformat()
    progress = progress_query(phase_id, today=day, member_ids=member_ids).subquery()
    end = func.min(func.coalesce(func.date(progress.c.deadline_at), day), day)
    missing = func.max(
        0, func.julianday(end) - func.julianday(func.date(progress.c.published_at)) + 1 - progress.c.checkin_count
    )
    below = (progress.c.total > 0) & (progress.c.solved < progress.c.total * threshold)
    return (
        select(Member.id.label("member_id"), Member.name.label("member_name"), Member.student_no,
               progress.c.id.label("task_id"), progress.c.title.label("task_title"), progress.c.phase_id,
               progress.c.total, progress.c.solved, missing.label("missing_days"),
               below.label("below_threshold"), progress.c.deadline_at)
        .join(Member, Member.id == progress.c.member_id)
        .where(Member.deleted_at.is_(None), Member.member_status.notin_(["已退会", "已除名"]),
               below | (missing >= missing_min))
        .order_by(progress.c.phase_id, progress.c.id, Member.id)
    )


def alerts(session, phase_id, threshold, *, today=None, missing_min=2):
    statement = alert_query(phase_id, threshold, today=today, missing_min=missing_min)
    return alert_items(session, statement)


def alert_items(session, statement):
    results = []
    for row in session.execute(statement).mappings():
        item = dict(row)
        item["missing_days"] = int(item["missing_days"])
        item["rate"] = round(item["solved"] / item["total"], 4) if item["total"] else 0.0
        results.append(item)
    return results


def makeup_checkin(session, member_id: int, task_id: int, day: str, note: str | None,
                   operator_id: int):
    """补卡按 (member, task, date) 查找：无记录则建，有记录则改状态与备注.

    任何情况下都不产生第二行；返回 (记录, 是否新建)。
    """
    existing = session.scalar(select(Checkin).where(
        Checkin.member_id == member_id, Checkin.task_id == task_id, Checkin.checkin_date == day,
    ))
    if existing is not None:
        existing.status = "补卡"
        if note is not None:
            existing.note = note
        session.flush()
        return existing, False
    row = Checkin(
        task_id=task_id, member_id=member_id, checkin_date=day, status="补卡",
        source="手工", note=note, created_at=local_time(),
    )
    session.add(row)
    session.flush()
    return row, True
