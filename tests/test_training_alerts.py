from datetime import date

from app.db import SessionLocal
from app.models import SysConfig, TrainingTask
from app.training import alerts

TODAY = date(2026, 11, 22)  # 周日，前一天也计入缺卡。


def _publish_task(api, problem_count, *, plan=None, phase_name="基础", title="任务",
                  start_on="2026-09-01", end_on="2027-01-31"):
    if plan is None:
        plan = api["plan"]()
    phase_id = api["phase"](plan, name=phase_name, start_on=start_on, end_on=end_on)
    problem_ids = api["problems"](*(f"P{i}" for i in range(problem_count)))
    task_id = api["task"](phase_id, problem_ids, title=title)
    api["assign"](task_id, "指定会员", member_ids=[api["ids"]["student01"]])
    return plan, phase_id, task_id, problem_ids


def test_below_threshold_member_appears_at_or_above_not(training_api):
    api = training_api
    plan_id = api["plan"]()
    cases = []
    for total, solved in ((2, 1), (5, 3), (5, 4)):
        _, phase_id, task_id, problem_ids = _publish_task(
            api, total, plan=plan_id, start_on=f"2026-11-{total + solved:02}",
            end_on=f"2026-11-{total + solved:02}",
        )
        cases.append((total, solved, phase_id, task_id, problem_ids))
    for total, solved, phase_id, task_id, problem_ids in cases:
        with SessionLocal(bind=api["engine"]) as session, session.begin():
            session.get(TrainingTask, task_id).published_at = f"{TODAY} 09:00:00"
        api["checkin"]("student01", task_id, solved_count=solved, checkin_date=str(TODAY),
                       solved_problem_ids=problem_ids[:solved])
        with SessionLocal(bind=api["engine"]) as session:
            items = alerts(session, phase_id, 0.6, today=TODAY)
        if solved / total < 0.6:
            assert [(item["student_no"], item["rate"], item["missing_days"]) for item in items] == [
                ("student01", 0.5, 0),
            ]
        else:
            assert items == []


def test_missing_two_days_appears(training_api, monkeypatch):
    from datetime import datetime

    from app import training

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 22, 12, tzinfo=tz)

    monkeypatch.setattr(training, "datetime", FixedDateTime)
    api = training_api
    _, phase_id, task_id, _ = _publish_task(api, 1)
    with SessionLocal(bind=api["engine"]) as session, session.begin():
        session.get(TrainingTask, task_id).published_at = "2026-11-21 10:00:00"
        session.get(SysConfig, "training.alert_threshold").config_value = "0"

    response = api["client"].get(f"/training-alerts?phase_id={phase_id}")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["threshold"] == 0
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert (item["student_no"], item["missing_days"], item["below_threshold"]) == ("student01", 2, False)
    with SessionLocal(bind=api["engine"]) as session:
        assert alerts(session, phase_id, 0, today=TODAY, missing_min=3) == []


def test_alert_threshold_fallback_and_empty_package(training_api):
    from app.models import Problem
    from app.models.base import local_time

    api = training_api
    _, phase_id, task_id, [problem_id] = _publish_task(api, 1)
    with SessionLocal(bind=api["engine"]) as session, session.begin():
        session.get(TrainingTask, task_id).published_at = f"{TODAY} 09:00:00"
        session.get(Problem, problem_id).deleted_at = local_time()
        session.get(SysConfig, "training.alert_threshold").config_value = "NaN"
    response = api["client"].get(f"/training-alerts?phase_id={phase_id}")
    assert response.json()["data"]["threshold"] == 0.6
    with SessionLocal(bind=api["engine"]) as session:
        assert alerts(session, phase_id, 0.6, today=TODAY) == []
        [warning] = alerts(session, phase_id, 0.6, today=date(2026, 11, 23))
        assert (warning["total"], warning["missing_days"], warning["below_threshold"]) == (0, 2, False)
