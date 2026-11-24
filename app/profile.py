"""S6 档案与统计纯逻辑：聚合查询、CSV 契约与多维统计.

只做对象组装与查询，不依赖 FastAPI 请求上下文。CSV 契约：
UTF-8（BOM 由调用方加）、CRLF 换行、中文表头、五个档案节，个人导出追加综测汇总、节间空一行。
"""

import csv
import unicodedata
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select, true

from .assessment import current_term, term_interval
from .auth import allows
from .models import AssessmentTest, Department, Member, MemberAssessment, MemberAward, TestScore
from .training import active_members_query, progress_query

SHANGHAI = ZoneInfo("Asia/Shanghai")
LEFT_STATUSES = ("已退会", "已除名")

CSV_BASIC_HEADER = ["学号", "姓名", "部门", "会员类型", "会员状态", "入会日期", "离会日期", "邮箱", "手机"]
CSV_TRAINING_HEADER = ["任务", "完成率", "打卡次数", "最后打卡日期"]
CSV_ASSESSMENT_HEADER = ["学期", "结果", "考核时间", "备注"]
CSV_AWARD_HEADER = ["日期", "类型", "标题", "分值(分)", "事由"]
CSV_CONTEST_HEADER = ["日期", "赛事", "队伍", "名次", "奖项"]


def format_points(extra_points):
    """整数百分位 → 两位小数的分值文本，150 → 1.50，-50 → -0.50。"""
    return f"{(extra_points or 0) / 100:.2f}"


def can_view_sensitive(actor, target, codes):
    return actor.id == target.id or actor.role == "sys_admin" or (
        actor.role == "coach" and allows(codes, "member:read")
    )


def mask_contact(value, actor, target, codes):
    if can_view_sensitive(actor, target, codes):
        return value
    if value and actor.role == "dept_admin" and actor.dept_id is not None and actor.dept_id == target.dept_id:
        return value[:3] + "*" * (len(value) - 5) + value[-2:] if len(value) > 5 else "*" * len(value)
    return None


def award_fields(row, *, show_reason):
    fields = {
        "id": row.id, "member_id": row.member_id, "award_type": row.award_type,
        "title": row.title, "happened_on": row.happened_on, "extra_points": row.extra_points,
        "points": format_points(row.extra_points) if row.extra_points is not None else None,
    }
    if show_reason:
        fields["reason"] = row.reason
    return fields


def award_history():
    return select(MemberAward).order_by(MemberAward.happened_on.desc(), MemberAward.id.desc())


def awards_of(session, member_id, *, show_reason, offset, limit):
    return [
        award_fields(row, show_reason=show_reason)
        for row in session.scalars(
            award_history().where(MemberAward.member_id == member_id).offset(offset).limit(limit)
        )
    ]


def award_summary(session, member_id):
    return session.scalar(
        select(func.coalesce(func.sum(MemberAward.extra_points), 0)).where(
            MemberAward.member_id == member_id
        )
    )


def training_summaries(session, member_ids):
    progress = progress_query(None, member_ids=member_ids).subquery()
    rows = session.execute(
        select(progress.c.member_id, func.count(), func.sum(progress.c.total),
               func.sum(progress.c.solved), func.sum(progress.c.checkin_count),
               func.max(progress.c.last_date)).group_by(progress.c.member_id)
    )
    summaries = {
        member_id: {
            "task_count": 0, "total": 0, "solved": 0, "rate": 0.0,
            "checkin_count": 0, "last_date": None,
        }
        for member_id in member_ids
    }
    for member_id, tasks, total, solved, checkins, last_date in rows:
        summaries[member_id] = {
            "task_count": tasks, "total": total, "solved": solved,
            "rate": round(solved / total, 4) if total else 0.0,
            "checkin_count": checkins, "last_date": last_date,
        }
    return summaries


def training_summary(session, member_id):
    return training_summaries(session, [member_id])[member_id]


def assessment_records():
    return select(MemberAssessment).order_by(
        MemberAssessment.term.desc(), MemberAssessment.assessed_at.desc(), MemberAssessment.id.desc()
    )


def assessment_history(session, member_id, *, offset, limit):
    rows = session.scalars(
        assessment_records().where(MemberAssessment.member_id == member_id).offset(offset).limit(limit)
    )
    return [
        {
            "id": row.id, "term": row.term, "result": row.result,
            "assessed_at": row.assessed_at, "remark": row.remark,
            "in_watchlist": row.in_watchlist,
        }
        for row in rows
    ]


def csv_cell(value):
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, str) and value:
        first = value[0]
        if first in "=+-@" or first.isspace() or unicodedata.category(first).startswith("C"):
            return "'" + value
    return value


def render_csv_sections(sections):
    class RowBuffer:
        def write(self, row):
            return row

    writer = csv.writer(RowBuffer(), lineterminator="\r\n")
    for index, (title, header, rows) in enumerate(sections):
        if index:
            yield writer.writerow([])
        yield writer.writerow([csv_cell(title)])
        yield writer.writerow(map(csv_cell, header))
        for row in rows:
            yield writer.writerow(map(csv_cell, row))


def _group_records(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.member_id].append(row)
    return grouped


def _score_value(raw):
    try:
        value = Decimal(raw) if raw is not None else None
    except InvalidOperation:
        return None
    return value if value is not None and value.is_finite() else None


def _assessment_csv_rows(assessments, scores):
    rows = [[row.term, row.result, row.assessed_at, row.remark] for row in assessments] or [["无记录"]]
    if scores:
        rows.append([])
        rows.append(["测评", "测评日期", "分值(分)"])
        total = Decimal(0)
        for date, title, raw in scores:
            score = _score_value(raw)
            rows.append([title, date, score])
            total += score or Decimal(0)
        rows.append(["测评总分", "", total])
    return rows


def _points(value):
    return Decimal(value) / 100 if value is not None else None


def _term_csv_section(awards, term, interval):
    row = ["未指定学期", "", "", ""]
    if interval is not None:
        start, end = (day.isoformat() for day in interval)
        totals = {"奖励": 0, "惩处": 0}
        for award in awards:
            if start <= award.happened_on <= end:
                totals[award.award_type] += award.extra_points or 0
        reward, penalty = totals["奖励"], totals["惩处"]
        row = [term, _points(reward), _points(penalty), _points(reward + penalty)]
    return ("[综测汇总]", ["学期", "奖励合计", "惩处合计", "净分"], [row])


def profile_csv_sections(session, targets, actor, codes, *, include_term_summary=False, term=None, interval=None):
    member_ids = [member.id for member, _ in targets]
    summaries = training_summaries(session, member_ids)
    assessments = _group_records(session.scalars(
        assessment_records().where(MemberAssessment.member_id.in_(member_ids))
    ))
    awards = _group_records(session.scalars(
        award_history().where(MemberAward.member_id.in_(member_ids))
    ))
    scores = defaultdict(list)
    for member_id, day, title, score in session.execute(
        select(TestScore.member_id, AssessmentTest.held_on, AssessmentTest.title, TestScore.score)
        .join(AssessmentTest).where(TestScore.member_id.in_(member_ids))
        .order_by(AssessmentTest.held_on.desc(), AssessmentTest.id.desc())
    ):
        scores[member_id].append((day, title, score))
    for member, department in targets:
        summary = summaries[member.id]
        show_reason = can_view_sensitive(actor, member, codes)
        basic = [
            member.student_no, member.name, department, member.member_type, member.member_status,
            member.joined_on, member.left_on, mask_contact(member.email, actor, member, codes),
            mask_contact(member.phone, actor, member, codes),
        ]
        training = [
            summary["task_count"], f"{summary['rate'] * 100:.2f}%",
            summary["checkin_count"], summary["last_date"],
        ]
        yield ("[基本信息]", CSV_BASIC_HEADER, [basic])
        yield ("[训练]", CSV_TRAINING_HEADER, [training])
        yield ("[考核]", CSV_ASSESSMENT_HEADER, _assessment_csv_rows(assessments[member.id], scores[member.id]))
        yield ("[奖惩]", CSV_AWARD_HEADER, [
            [row.happened_on, row.award_type, row.title, _points(row.extra_points),
             row.reason if show_reason else None]
            for row in awards[member.id]
        ] or [["无记录"]])
        yield ("[赛事]", CSV_CONTEST_HEADER, [["无记录"]])
        if include_term_summary:
            yield _term_csv_section(awards[member.id], term, interval)


def full_csv_sections(session, actor, codes):
    statement = active_members_query().add_columns(Department.name).outerjoin(Department).order_by(Member.student_no)
    last_student_no = None
    while True:
        batch = statement if last_student_no is None else statement.where(Member.student_no > last_student_no)
        targets = session.execute(batch.limit(100)).all()
        if not targets:
            return
        yield from profile_csv_sections(session, targets, actor, codes)
        last_student_no = targets[-1][0].student_no


def _distribution(session, column, scope):
    """单维分布：以取值域为左表，外连接在册计数，零值项保留。"""
    domain = select(column.label("value")).where(scope, column.isnot(None)).distinct().subquery()
    counts = (
        select(column.label("value"), func.count().label("count"))
        .where(
            Member.deleted_at.is_(None), Member.member_status.notin_(LEFT_STATUSES),
            scope, column.isnot(None),
        )
        .group_by(column).subquery()
    )
    rows = session.execute(
        select(domain.c.value.label("value"), func.coalesce(counts.c.count, 0).label("count"))
        .select_from(domain).outerjoin(counts, counts.c.value == domain.c.value)
        .order_by(domain.c.value)
    ).all()
    return [{"value": value, "count": int(count)} for value, count in rows]


def _department_distributions(session, scope, department_scope):
    counts = (
        select(Member.dept_id, Member.member_type, func.count().label("count"))
        .where(Member.deleted_at.is_(None), Member.member_status.notin_(LEFT_STATUSES), scope)
        .group_by(Member.dept_id, Member.member_type).subquery()
    )
    departments = session.execute(
        select(Department.name, func.coalesce(func.sum(counts.c.count), 0))
        .outerjoin(counts, counts.c.dept_id == Department.id)
        .where(department_scope).group_by(Department.id).order_by(Department.name)
    )
    types = select(Member.member_type).where(scope, Member.member_type.isnot(None)).distinct().subquery()
    combinations = session.execute(
        select(Department.name, types.c.member_type, func.coalesce(counts.c.count, 0))
        .select_from(Department).join(types, true())
        .outerjoin(counts, and_(counts.c.dept_id == Department.id, counts.c.member_type == types.c.member_type))
        .where(department_scope).order_by(Department.name, types.c.member_type)
    )
    return (
        [{"value": name, "count": count} for name, count in departments],
        [{"department": name, "member_type": kind, "count": count} for name, kind, count in combinations],
    )


def member_stats(session, term=None, *, scope=true(), department_scope=true()):
    selected_term = term or current_term(session)
    start, end = term_interval(selected_term)
    active = session.scalar(
        select(func.count()).select_from(Member).where(
            Member.deleted_at.is_(None), Member.member_status.notin_(LEFT_STATUSES), scope,
        )
    ) or 0
    new_count = session.scalar(
        select(func.count()).select_from(Member).where(
            Member.deleted_at.is_(None), Member.joined_on.isnot(None),
            Member.joined_on.between(start.isoformat(), end.isoformat()), scope,
        )
    ) or 0
    left_count = session.scalar(
        select(func.count()).select_from(Member).where(
            Member.deleted_at.is_(None), Member.member_status.in_(LEFT_STATUSES),
            Member.left_on.isnot(None),
            Member.left_on.between(start.isoformat(), end.isoformat()), scope,
        )
    ) or 0
    departments, combinations = _department_distributions(session, scope, department_scope)
    return {
        "term": selected_term,
        "active": active,
        "new_this_term": new_count,
        "left_this_term": left_count,
        "distributions": {
            "grade": _distribution(session, Member.grade, scope),
            "major": _distribution(session, Member.major, scope),
            "department": departments,
            "member_type": _distribution(session, Member.member_type, scope),
            "department_member_type": combinations,
        },
    }


def csv_filename_for(member):
    return f"profile-{member.student_no}-{datetime.now(SHANGHAI).strftime('%Y%m%d')}.csv"
