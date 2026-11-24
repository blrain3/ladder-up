import json

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import AuditLog, Checkin


def _setup(api):
    plan_id = api["plan"]()
    phase_id = api["phase"](plan_id)
    problem_id = api["problems"]("P1")[0]
    task_id = api["task"](phase_id, [problem_id])
    api["assign"](task_id, "指定会员", member_ids=[api["ids"]["student01"]])
    return task_id


def test_makeup_creates_row_when_missing(training_api):
    api = training_api
    task_id = _setup(api)
    csrf = api["member_csrf"]("coach0001")
    response = api["client"].post(
        "/checkins/makeup",
        json={"task_id": task_id, "member_id": api["ids"]["student01"],
              "checkin_date": "2026-11-20", "note": "赛后补卡"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["created"] is True
    assert response.json()["data"]["status"] == "补卡"
    with SessionLocal(bind=api["engine"]) as session:
        checkin = session.scalar(select(Checkin).where(
            Checkin.task_id == task_id, Checkin.member_id == api["ids"]["student01"],
            Checkin.checkin_date == "2026-11-20",
        ))
        assert checkin is not None and checkin.status == "补卡"
        assert session.scalar(select(func.count()).select_from(Checkin)) == 1
        assert checkin.note == "赛后补卡"
        log = session.scalar(select(AuditLog).where(AuditLog.action == "checkin.makeup"))
        assert log.target_id == checkin.id
        assert json.loads(log.after_json)["note"] == "赛后补卡"


def test_makeup_updates_existing_without_new_row(training_api):
    api = training_api
    task_id = _setup(api)
    api["checkin"]("student01", task_id, solved_count=0, checkin_date="2026-11-20")
    with SessionLocal(bind=api["engine"]) as session:
        before = session.scalar(select(func.count()).select_from(Checkin).where(
            Checkin.task_id == task_id, Checkin.member_id == api["ids"]["student01"],
            Checkin.checkin_date == "2026-11-20",
        ))
    assert before == 1

    csrf = api["member_csrf"]("coach0001")
    response = api["client"].post(
        "/checkins/makeup",
        json={"task_id": task_id, "member_id": api["ids"]["student01"],
              "checkin_date": "2026-11-20", "note": "状态改为补卡"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["created"] is False

    with SessionLocal(bind=api["engine"]) as session:
        after = session.scalar(select(func.count()).select_from(Checkin).where(
            Checkin.task_id == task_id, Checkin.member_id == api["ids"]["student01"],
            Checkin.checkin_date == "2026-11-20",
        ))
        checkin = session.scalar(select(Checkin).where(
            Checkin.task_id == task_id, Checkin.member_id == api["ids"]["student01"],
            Checkin.checkin_date == "2026-11-20",
        ))
    assert after == 1
    assert checkin.status == "补卡"
    assert checkin.note == "状态改为补卡"


def test_makeup_requires_approve_403(training_api):
    api = training_api
    task_id = _setup(api)
    csrf = api["member_csrf"]("student01")
    response = api["client"].post(
        "/checkins/makeup",
        json={"task_id": task_id, "member_id": api["ids"]["student01"],
              "checkin_date": "2026-11-20"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 403
    assert response.json()["code"] == 40301
    with SessionLocal(bind=api["engine"]) as session:
        denial = session.scalar(select(AuditLog).where(
            AuditLog.action == "auth.permission.denied"
        ))
        assert denial is not None and denial.target_type == "training"


def test_makeup_rejects_draft_unassigned_and_future_without_writes(training_api):
    api = training_api
    phase = api["phase"](api["plan"]())
    problem_ids = api["problems"]("待补题")
    draft = api["task"](phase, problem_ids)
    published = api["task"](phase, problem_ids)
    api["assign"](published, "指定会员", member_ids=[api["ids"]["student01"]])
    today = "2026-11-20"
    for task, target, day, status, code in (
        (draft, api["ids"]["student01"], today, 400, 40002),
        (published, api["ids"]["student02"], today, 400, 40002),
        (published, api["ids"]["student01"], "2100-01-01", 400, 40001),
        (published, 99999, today, 400, 40002),
        (99999, api["ids"]["student01"], today, 404, 40401),
    ):
        response = api["client"].post(
            "/checkins/makeup", headers={"X-CSRF-Token": api["coach_csrf"]},
            json={"task_id": task, "member_id": target, "checkin_date": day},
        )
        assert (response.status_code, response.json()["code"]) == (status, code)
    with SessionLocal(bind=api["engine"]) as session:
        assert session.scalar(select(func.count()).select_from(Checkin)) == 0
        assert session.scalar(select(AuditLog).where(AuditLog.action == "checkin.makeup")) is None
