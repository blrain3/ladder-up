"""S5 考核纯逻辑：录分、考核计算、转正、退会与除名.

只做对象组装与查询，不依赖 FastAPI 请求上下文。日期统一使用 Asia/Shanghai
本地时间；term 格式与训练计划一致，分数统一存为最多两位小数的字符串。
"""

import json
import logging
import re
from calendar import monthrange
from datetime import date, datetime
from math import isfinite
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert

from .models import AssessmentTest, AuditLog, Member, MemberAssessment, SysConfig, TestScore
from .models.base import local_time
from .security import revoke_all_sessions
from .training import progress_query

SHANGHAI = ZoneInfo("Asia/Shanghai")
PASS_SCORE = 60.0
MAX_SCORE = 9999.99
MAX_SOLVED = 2**31 - 1
ACTIVE_STATUSES = {"报名中", "待审核", "观察会员", "正式会员", "竞赛实验室成员"}
LEFT_STATUSES = {"已退会", "已除名"}


class AssessmentError(Exception):
    def __init__(self, message, *, code=40001, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


def term_interval(term):
    """学期区间含首尾，与训练计划使用同一套格式。"""
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
    raise ValueError("学期格式或年份无效")


def current_term(session):
    """当前学期：优先 sys_config 的 academic_year.current，再按上海日期推导。"""
    raw = session.scalar(select(SysConfig.config_value).where(
        SysConfig.config_key == "academic_year.current"
    ))
    today = datetime.now(SHANGHAI).date()
    match = re.fullmatch(r"([0-9]{4})-([0-9]{4})", raw or "")
    if match:
        year1, year2 = int(match[1]), int(match[2])
        if 2000 <= year1 <= 2100 and year2 == year1 + 1:
            suffix = "1" if today.month >= 9 or today.month <= 1 else "2"
            return f"{year1}-{year2}-{suffix}"
    if today.month >= 9:
        return f"{today.year}-{today.year + 1}-1"
    if today.month <= 1:
        return f"{today.year - 1}-{today.year}-1"
    return f"{today.year - 1}-{today.year}-2"


def promote_threshold(session):
    raw = session.scalar(select(SysConfig.config_value).where(
        SysConfig.config_key == "training.promote_threshold"
    ))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.6
    return value if 0 <= value <= 1 else 0.6


def _write_audit(session, action, *, actor_id, target_id, target_type, before=None, after=None):
    session.add(AuditLog(
        actor_id=actor_id, action=action, target_type=target_type, target_id=target_id,
        ip=None, request_id=None, created_at=local_time(),
        before_json=json.dumps(before, ensure_ascii=False) if before is not None else None,
        after_json=json.dumps(after, ensure_ascii=False) if after is not None else None,
    ))


def _parse_id(value, label="member_id"):
    if isinstance(value, bool):
        raise AssessmentError(f"{label}无效", code=40001)
    if isinstance(value, int):
        if 1 <= value <= 2**63 - 1:
            return value
        raise AssessmentError(f"{label}无效", code=40001)
    if isinstance(value, str) and value.strip().isdecimal():
        number = int(value.strip())
        if 1 <= number <= 2**63 - 1:
            return number
    raise AssessmentError(f"{label}无效", code=40001)


def _solved_field(value):
    if value in (None, ""):
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise AssessmentError("已解出题数格式不正确", code=40001) from None
    if not 0 <= number <= MAX_SOLVED:
        raise AssessmentError(f"已解出题数须在 0–{MAX_SOLVED} 之间", code=40001)
    return number


def _score_field(value):
    if value in (None, ""):
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        raise AssessmentError("分数格式不正确", code=40001) from None
    if not 0 <= number <= MAX_SCORE:
        raise AssessmentError(f"分数须在 0–{MAX_SCORE} 之间", code=40001)
    return number


def normalize_kp_snapshot(raw):
    """A snapshot maps knowledge-point names to correct and total question counts."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise AssessmentError("知识点答题数据格式不正确") from exc
    if not isinstance(raw, dict) or len(raw) > 100:
        raise AssessmentError("知识点答题数据须为最多 100 个知识点的对象")
    for name, counts in raw.items():
        if not isinstance(name, str) or not name.strip() or len(name) > 100 or not isinstance(counts, dict):
            raise AssessmentError("知识点名称或答题数格式不正确")
        correct, total = counts.get("correct"), counts.get("total")
        if type(correct) is not int or type(total) is not int or not 0 <= correct <= total <= MAX_SOLVED or not total:
            raise AssessmentError("知识点答对数须介于 0 和总题数之间，总题数须为正整数")
    return {name: {"correct": counts["correct"], "total": counts["total"]} for name, counts in raw.items()}


def parse_kp_snapshot(raw):
    try:
        return normalize_kp_snapshot(raw)
    except AssessmentError:
        return {}


def record_scores(session, test_id, rows, operator_id, scope) -> dict:
    """Apply score changes against the submitted row revisions and audit each write."""
    test = session.get(AssessmentTest, test_id)
    if test is None:
        raise AssessmentError("测评不存在", code=40401, status=404)
    created = 0
    updated = 0
    for row in rows:
        if not isinstance(row, dict):
            raise AssessmentError("成绩数据格式不正确", code=40001)
        member_id = _parse_id(row.get("member_id"))
        member = session.scalar(select(Member).where(Member.id == member_id, scope))
        if member is None:
            raise AssessmentError("会员不存在", code=40401, status=404)
        if member.deleted_at is not None or member.member_status in LEFT_STATUSES:
            raise AssessmentError("会员不存在或已离会", code=40002)
        solved = _solved_field(row.get("solved_count"))
        score_value = _score_field(row.get("score"))
        score_text = f"{score_value:.2f}" if score_value is not None else None

        existing = session.scalar(select(TestScore).where(
            TestScore.test_id == test_id, TestScore.member_id == member_id,
        ))
        version = row.get("version")
        if str(version) != str(existing.version if existing else 0):
            raise AssessmentError("成绩已变更，请刷新后重试", code=40902, status=409)
        snapshot = existing.kp_snapshot if existing else None
        before = {"solved_count": existing.solved_count, "score": existing.score} if existing else None
        values = {"solved_count": solved, "score": score_text}
        if "kp_snapshot" in row:
            parsed = normalize_kp_snapshot(row["kp_snapshot"])
            snapshot = json.dumps(parsed, ensure_ascii=False, sort_keys=True) if parsed else None
            values["kp_snapshot"] = snapshot
            if before is not None:
                before["kp_snapshot"] = existing.kp_snapshot
        if existing is None:
            score_row = TestScore(
                test_id=test_id, member_id=member_id, solved_count=solved,
                score=score_text, kp_snapshot=snapshot,
            )
            session.add(score_row)
            session.flush()
            _write_audit(
                session, "score.created", actor_id=operator_id, target_id=score_row.id,
                target_type="test_score",
                after={"test_id": test_id, "member_id": member_id,
                       **values},
            )
            created += 1
        else:
            if before == values:
                continue
            result = session.execute(update(TestScore).where(
                TestScore.id == existing.id, TestScore.version == int(version),
            ).values(**values, version=TestScore.version + 1))
            if result.rowcount != 1:
                raise AssessmentError("成绩已变更，请刷新后重试", code=40902, status=409)
            _write_audit(
                session, "score.updated", actor_id=operator_id, target_id=existing.id,
                target_type="test_score", before=before,
                after={"test_id": test_id, "member_id": member_id,
                       **values, "updated": True},
            )
            updated += 1
    return {"created": created, "updated": updated}


def _test_scores(session, member_id, term):
    start, end = term_interval(term)
    tests = session.scalars(select(AssessmentTest).where(
        AssessmentTest.held_on.between(start.isoformat(), end.isoformat())
    ).order_by(AssessmentTest.id)).all()
    scores = {test.id: 0.0 for test in tests}
    if tests:
        rows = session.scalars(select(TestScore).where(
            TestScore.test_id.in_([test.id for test in tests]),
            TestScore.member_id == member_id,
        ))
        for row in rows:
            try:
                score = float(row.score or 0)
                if not isfinite(score):
                    raise ValueError("non-finite score")
                scores[row.test_id] = score
            except (TypeError, ValueError):
                logging.getLogger(__name__).warning("Invalid score for assessment test %s", row.test_id)
                scores[row.test_id] = 0.0
    return tests, scores


def assess_member(session, member_id, term) -> str:
    """计算单个会员的学期考核结果并写入 member_assessment（存在则更新）。"""
    term_interval(term)
    tests, scores = _test_scores(session, member_id, term)
    if not tests:
        result = "不通过"
    else:
        average = sum(scores.values()) / len(tests)
        result = "通过" if average >= PASS_SCORE else "不通过"

    values = {"result": result, "in_watchlist": int(result == "不通过"), "assessed_at": local_time()}
    session.execute(insert(MemberAssessment).values(
        member_id=member_id, term=term, **values,
    ).on_conflict_do_update(index_elements=["member_id", "term"], set_=values))
    return result


def promotion_items(session, members, term):
    from .models import TrainingPhase, TrainingPlan

    ids = [member.id for member in members]
    if not ids:
        return []
    assessments = dict(session.execute(select(MemberAssessment.member_id, MemberAssessment.result).where(
        MemberAssessment.member_id.in_(ids), MemberAssessment.term == term,
    )).all())
    progress = progress_query(None, member_ids=ids).subquery()
    statement = (
        select(progress.c.member_id, func.sum(progress.c.solved), func.sum(progress.c.total))
        .join(TrainingPhase, TrainingPhase.id == progress.c.phase_id)
        .join(TrainingPlan, TrainingPlan.id == TrainingPhase.plan_id)
        .where(TrainingPlan.term == term).group_by(progress.c.member_id)
    )
    completion = {member_id: (solved, total) for member_id, solved, total in session.execute(statement)}
    threshold = promote_threshold(session)
    items = []
    for member in members:
        result = assessments.get(member.id)
        solved, total = completion.get(member.id, (0, 0))
        rate = solved / total if total else 0.0
        reasons = []
        if member.member_status != "观察会员":
            reasons.append(f"当前状态为「{member.member_status}」，仅观察会员可转正")
        if result != "通过":
            reasons.append("该学期考核未通过")
        if not total:
            reasons.append("训练完成率不足（该学期无已下发训练任务）")
        elif rate < threshold:
            reasons.append(f"训练完成率不足（当前 {rate:.4f}，要求 ≥ {threshold:.4f}）")
        items.append({
            "id": member.id, "student_no": member.student_no, "name": member.name,
            "member_status": member.member_status, "assessment_result": result,
            "completion_rate": rate, "completion_solved": solved, "completion_total": total,
            "ready": not reasons, "reasons": reasons,
        })
    return items


def promote_ready(session, member_id, term):
    term_interval(term)
    member = session.get(Member, member_id)
    if member is None or member.deleted_at is not None:
        return False, ["会员不存在"]
    item = promotion_items(session, [member], term)[0]
    return item["ready"], item["reasons"]


def transition_member(session, member, status, **values):
    result = session.execute(update(Member).where(
        Member.id == member.id, Member.member_status == member.member_status,
        Member.deleted_at.is_(None),
    ).values(member_status=status, updated_at=local_time(), **values))
    if result.rowcount != 1:
        raise AssessmentError("会员状态已变更，请刷新后重试", code=40002)


def promote(session, member_id, operator_id, selected_term=None) -> None:
    """校验后转正：写 member_status / member_type 并审计 member.promote。"""
    if session.get(Member, member_id) is None:
        raise AssessmentError("会员不存在", code=40401, status=404)
    term = current_term(session)
    if selected_term is not None and selected_term != term:
        raise AssessmentError("历史学期只读", code=40002)
    ready, reasons = promote_ready(session, member_id, term)
    if not ready:
        raise AssessmentError("；".join(reasons), code=40002)
    member = session.get(Member, member_id)
    before = {"member_status": member.member_status, "member_type": member.member_type}
    transition_member(session, member, "正式会员", member_type="正式会员")
    _write_audit(
        session, "member.promote", actor_id=operator_id, target_id=member_id,
        target_type="member", before=before,
        after={"member_status": "正式会员", "member_type": "正式会员", "term": term},
    )


def active_target(session, member_id, *, action):
    member = session.get(Member, member_id)
    if member is None or member.deleted_at is not None:
        raise AssessmentError("会员不存在", code=40401, status=404)
    if member.member_status not in ACTIVE_STATUSES:
        raise AssessmentError(f"该会员状态为「{member.member_status}」，不能重复{action}", code=40002)
    return member


def withdraw(session, member_id, operator_id, reason, *, action="member.withdraw") -> None:
    """退会：撤销全部会话并审计 member.withdraw。"""
    member = active_target(session, member_id, action="退会")
    now = local_time()
    transition_member(session, member, "已退会", left_on=now[:10])
    revoke_all_sessions(member_id, reason="退会", session=session)
    session.flush()
    _write_audit(
        session, action, actor_id=operator_id, target_id=member_id,
        target_type="member",
        after={"reason": reason, "member_status": "已退会", "left_on": member.left_on},
    )


def expel(session, member_id, operator_id, reason) -> None:
    """除名：事由必填（10–500 字），撤销全部会话并审计 member.expel。"""
    if not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 500:
        raise AssessmentError("除名事由须为 10–500 字", code=40001)
    reason = reason.strip()
    member = active_target(session, member_id, action="除名")
    now = local_time()
    transition_member(session, member, "已除名", left_on=now[:10])
    revoke_all_sessions(member_id, reason="除名", session=session)
    session.flush()
    _write_audit(
        session, "member.expel", actor_id=operator_id, target_id=member_id,
        target_type="member",
        after={"reason": reason, "member_status": "已除名", "left_on": member.left_on},
    )
