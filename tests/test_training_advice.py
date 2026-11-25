import json
from datetime import date, timedelta

import pytest

from app.advice import training_advice
from app.db import SessionLocal
from app.models import AssessmentTest, Checkin, CheckinProblem, SysConfig, TestScore
from app.models.base import local_time

TODAY = date(2026, 11, 25)


def day_ago(days):
    return (TODAY - timedelta(days=days)).isoformat()


@pytest.fixture
def task_data(training_api):
    api = training_api
    owner = api["ids"]["student01"]
    task = api["task"](api["phase"](api["plan"]()), api["problems"]("甲题", "乙题"))
    api["assign"](task, "指定会员", member_ids=[owner])
    return api["engine"], owner, task


def add_checkin(session, owner, task, days, status="打卡"):
    row = Checkin(member_id=owner, task_id=task, checkin_date=day_ago(days),
                  solved_count=0, status=status, source="手工", created_at=local_time())
    session.add(row)
    session.flush()
    return row


@pytest.mark.parametrize(("days", "expected"), [(2, True), (1, False), (14, False)])
def test_inactivity_is_bounded_and_ignores_explicit_missing_rows(task_data, days, expected):
    engine, owner, task = task_data
    with SessionLocal(bind=engine) as session, session.begin():
        add_checkin(session, owner, task, days)
        add_checkin(session, owner, task, 0, "缺卡")
        items = {row["rule"]: row for row in training_advice(session, owner, TODAY)}
        assert ("inactive" in items) == expected
        if expected:
            assert items["inactive"]["basis"]["days"] == days
            assert "按自然日，含今天" in items["inactive"]["text"]


@pytest.mark.parametrize(("threshold", "expected"), [("0.6", True), ("0.5", False), ("0.50001", True), ("NaN", True), ("inf", True), ("bad", True), (None, True)])
def test_completion_threshold_uses_individual_task_problems(task_data, threshold, expected):
    from sqlalchemy import select

    from app.models import TaskProblem, TrainingTask

    engine, owner, task = task_data
    with SessionLocal(bind=engine) as session, session.begin():
        session.get(TrainingTask, task).published_at = day_ago(3) + " 12:00:00"
        config = session.get(SysConfig, "training.promote_threshold")
        if threshold is None:
            session.delete(config)
        else:
            config.config_value = threshold
        problem = session.scalar(select(TaskProblem.problem_id).where(TaskProblem.task_id == task))
        for days in (0, 1):
            checkin = add_checkin(session, owner, task, days)
            session.add(CheckinProblem(checkin_id=checkin.id, problem_id=problem, solved=1))
        items = {row["rule"]: row for row in training_advice(session, owner, TODAY)}
        assert ("completion" in items) == expected
        if expected:
            assert items["completion"]["basis"]["solved"] == 1
            assert items["completion"]["basis"]["total"] == 2
            assert items["completion"]["text"] == "完成率 1/2（50.00%），未达阈值"
            assert items["completion"]["basis"]["threshold"] == (
                0.50001 if threshold == "0.50001" else 0.6
            )


def add_score(session, member_id, days, correct, *, point="动态规划"):
    test = AssessmentTest(title="周测", test_type="周测", held_on=day_ago(days), created_at=local_time())
    session.add(test)
    session.flush()
    session.add(TestScore(
        test_id=test.id, member_id=member_id,
        kp_snapshot=json.dumps({point: {"correct": correct, "total": 5}}) if correct is not None else None,
    ))


@pytest.mark.parametrize(("correct", "expected"), [(2, True), (3, False)])
def test_weakest_point_uses_latest_three_assessments(engine, accounts, correct, expected):
    owner = accounts[0]["student01"]
    with SessionLocal(bind=engine) as session, session.begin():
        for days in (0, 1, 2):
            add_score(session, owner, days, correct)
        add_score(session, owner, 3, 0 if not expected else 5)
        result = [item for item in training_advice(session, owner, TODAY) if item["rule"] == "knowledge_point"]
        assert bool(result) == expected
        if expected:
            assert result[0]["basis"]["correct"] == 6
            assert result[0]["basis"]["total"] == 15
            assert "动态规划" in result[0]["text"]
            assert "近 14 天内最近 3 次测评，样本 3 次" in result[0]["text"]


def test_no_recent_samples_and_no_leakage_between_members(engine, accounts):
    ids, _ = accounts
    with SessionLocal(bind=engine) as session, session.begin():
        add_score(session, ids["student02"], 0, 0, point="他人知识点")
        add_score(session, ids["student01"], 14, 0)
        assert training_advice(session, ids["student01"], TODAY) == []


def test_score_api_records_snapshot_for_advice(assessment_api):
    api = assessment_api
    owner = api["ids"]["student01"]
    test_id = api["create_test"](held_on=day_ago(0))
    result = api["record"](test_id, [{
        "member_id": owner, "score": 70, "kp_snapshot": {"搜索": {"correct": 1, "total": 4}},
    }])
    assert result["created"] == 1
    with SessionLocal(bind=api["engine"]) as session:
        item, = training_advice(session, owner, TODAY)
        assert item["basis"]["rate"] == 0.25
