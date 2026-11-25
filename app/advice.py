"""Deterministic training advice based on local days and recorded results."""

from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import and_, func, select

from .assessment import SHANGHAI, parse_kp_snapshot, promote_threshold
from .models import (
    AssessmentTest,
    Checkin,
    CheckinProblem,
    Problem,
    TaskProblem,
    TestScore,
    TrainingTask,
)
from .training import member_tasks_query


def completion(session, member_id, start, end):
    tasks = member_tasks_query(member_id).where(
        func.date(TrainingTask.published_at).between(start, end),
    ).with_only_columns(TrainingTask.id, func.date(TrainingTask.published_at).label("day")).subquery()
    done = (
        select(Checkin.task_id, CheckinProblem.problem_id)
        .join(CheckinProblem).where(
            Checkin.member_id == member_id, Checkin.checkin_date.between(start, end),
            Checkin.status.in_(("打卡", "补卡")), CheckinProblem.solved == 1,
        ).distinct().subquery()
    )
    return session.execute(
        select(func.count(TaskProblem.problem_id), func.count(done.c.problem_id), func.min(tasks.c.day))
        .select_from(tasks).outerjoin(TaskProblem, TaskProblem.task_id == tasks.c.id)
        .outerjoin(Problem, Problem.id == TaskProblem.problem_id)
        .outerjoin(done, and_(done.c.task_id == tasks.c.id, done.c.problem_id == TaskProblem.problem_id))
        .where(Problem.deleted_at.is_(None))
    ).one()


def weakest_point(scores):
    totals = defaultdict(lambda: [0, 0, 0])
    for _, raw in scores:
        for name, count in parse_kp_snapshot(raw).items():
            aggregate = totals[name]
            aggregate[0] += count["correct"]
            aggregate[1] += count["total"]
            aggregate[2] += 1
    if not totals:
        return None
    name, (correct, total, assessments) = min(
        totals.items(), key=lambda item: (item[1][0] / item[1][1], item[0]),
    )
    return {"knowledge_point": name, "correct": correct, "total": total,
            "rate": correct / total, "assessments": assessments}


def training_advice(session, member_id, today=None) -> list[dict]:
    today = today or datetime.now(SHANGHAI).date()
    start = (today - timedelta(days=13)).isoformat()
    end = today.isoformat()
    last_checkin = session.scalar(select(func.max(Checkin.checkin_date)).where(
        Checkin.member_id == member_id, Checkin.checkin_date.between(start, end),
        Checkin.status.in_(("打卡", "补卡")),
    ))
    total, solved, first_task = completion(session, member_id, start, end)
    scores = session.execute(
        select(AssessmentTest.held_on, TestScore.kp_snapshot).join(TestScore)
        .where(TestScore.member_id == member_id, AssessmentTest.held_on.between(start, end))
        .order_by(AssessmentTest.held_on.desc(), AssessmentTest.id.desc()).limit(3)
    ).all()

    items = []
    observed_since = min([day for day in [first_task, *(day for day, _ in scores)] if day], default=None)
    if last_checkin or observed_since:
        first_missing = (date.fromisoformat(last_checkin) + timedelta(days=1)
                         if last_checkin else date.fromisoformat(observed_since))
        days = (today - first_missing).days + 1
        if days >= 2:
            items.append({
                "rule": "inactive", "text": f"近 {days} 天没有打卡（按自然日，含今天），建议先补一次短时训练",
                "basis": {"days": days, "date_from": first_missing.isoformat(), "date_to": end},
            })

    if total:
        rate = solved / total
        threshold = promote_threshold(session)
        if rate < threshold:
            items.append({
                "rule": "completion", "text": f"完成率 {solved}/{total}（{rate * 100:.2f}%），未达阈值",
                "basis": {"solved": solved, "total": total, "rate": rate, "threshold": threshold,
                          "date_from": start, "date_to": end},
            })

    weakest = weakest_point(scores)
    if weakest and weakest["rate"] < 0.6:
        items.append({
            "rule": "knowledge_point",
            "text": f"知识点「{weakest['knowledge_point']}」正确率偏低，"
                    f"近 14 天内最近 3 次测评，样本 {weakest['assessments']} 次",
            "basis": {**weakest, "threshold": 0.6, "date_from": start, "date_to": end},
        })
    return items
