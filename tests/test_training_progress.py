from sqlalchemy import select

from app.db import SessionLocal
from app.models import Checkin, Problem
from app.models.base import local_time
from app.training import task_progress


def _published_task(api, problem_count):
    plan_id = api["plan"]()
    phase_id = api["phase"](plan_id)
    problem_ids = api["problems"](*(f"P{i}" for i in range(problem_count)))
    task_id = api["task"](phase_id, problem_ids)
    api["assign"](task_id, "指定会员", member_ids=[api["ids"]["student01"]])
    return task_id, problem_ids


def test_progress_derived_from_details(training_api):
    api = training_api
    task_id, problem_ids = _published_task(api, 3)
    api["checkin"](
        "student01", task_id, solved_count=1,
        problems_payload=[
            {"problem_id": problem_ids[0], "solved": 1, "attempts": 1},
            {"problem_id": problem_ids[1], "solved": 0, "attempts": 2},
            {"problem_id": problem_ids[2], "solved": 0, "attempts": None},
        ],
    )
    with SessionLocal(bind=api["engine"]) as session, session.begin():
        session.scalar(select(Checkin)).solved_count = 99
    with SessionLocal(bind=api["engine"]) as session:
        progress = task_progress(session, task_id, api["ids"]["student01"])
    assert (progress["total"], progress["solved"]) == (3, 1)
    assert progress["rate"] == round(1 / 3, 4)
    assert progress["checkins"][0]["solved_count"] == 1
    assert progress["checkins"][0]["inconsistent"] is True


def test_soft_deleted_problem_not_counted(training_api):
    api = training_api
    task_id, problem_ids = _published_task(api, 3)
    with SessionLocal(bind=api["engine"]) as session, session.begin():
        problem = session.get(Problem, problem_ids[0])
        problem.deleted_at = local_time()
    with SessionLocal(bind=api["engine"]) as session:
        progress = task_progress(session, task_id, api["ids"]["student01"])
    assert progress["total"] == 2


def test_unassigned_member_cannot_view_task_progress(training_api):
    api = training_api
    task_id, _ = _published_task(api, 1)
    api["member_csrf"]("student02")
    client = api["client"]
    response = client.get(f"/training-tasks/{task_id}")
    assert response.status_code == 404
    assert client.get("/training-progress").status_code == 200


def test_unassigned_member_cannot_view_others_progress(training_api):
    api = training_api
    _published_task(api, 1)
    api["member_csrf"]("student02")
    response = api["client"].get(
        f"/training-progress?member_id={api['ids']['student01']}"
    )
    assert response.status_code == 403


def test_progress_deduplicates_across_days_and_preserves_deleted_history(training_api):
    api = training_api
    task_id, [first, second] = _published_task(api, 2)
    for day, solved in (("2026-11-20", [first]), ("2026-11-21", [first, second])):
        api["checkin"]("student01", task_id, solved_count=len(solved),
                       solved_problem_ids=solved, checkin_date=day)
    summary = api["client"].get("/training-progress").json()["data"]
    assert (summary["solved"], summary["total"]) == (2, 2)
    with SessionLocal(bind=api["engine"]) as session, session.begin():
        session.get(Problem, first).deleted_at = local_time()
    with SessionLocal(bind=api["engine"]) as session:
        progress = task_progress(session, task_id, api["ids"]["student01"])
    assert (progress["solved"], progress["total"]) == (1, 1)
    assert len(progress["checkins"]) == 2
    assert progress["checkins"][1]["problems"][0]["problem_id"] == first


def test_department_scope_paginates_progress_and_alerts(training_api):
    api = training_api
    phase_id = api["phase"](api["plan"]())
    problem_ids = api["problems"]("部门训练题")
    task_id = api["task"](phase_id, problem_ids)
    api["assign"](task_id, "指定会员", member_ids=[api["ids"]["student01"], api["ids"]["student02"]])
    client = api["client"]

    api["member_csrf"]("deptadmin")
    first = client.get("/training-progress?page_size=1").json()["data"]
    second = client.get("/training-progress?page_size=1&page=2").json()["data"]
    assert [item["member_id"] for item in first["items"] + second["items"]] == [
        api["ids"]["student01"], api["ids"]["deptadmin"],
    ]
    third = client.get("/training-progress?page_size=1&page=3").json()["data"]
    assert first["has_next"] and second["has_next"] and not third["has_next"]
    assert [item["member_id"] for item in third["items"]] == [api["coach_id"]]
    denied = client.get("/training-progress", params={"member_id": api["ids"]["student02"]})
    assert denied.status_code == 403
    assert [item["student_no"] for item in client.get("/training-alerts").json()["data"]["items"]] == ["student01"]

    api["member_csrf"]("coach0001")
    first = client.get("/training-alerts?page_size=1").json()["data"]
    second = client.get("/training-alerts?page_size=1&page=2").json()["data"]
    assert first["has_next"] and not second["has_next"]
    assert [item["student_no"] for item in first["items"] + second["items"]] == ["student01", "student02"]


def test_task_and_checkin_list_filters_and_pages(training_api):
    api = training_api
    phase_id = api["phase"](api["plan"]())
    problem_ids = api["problems"]("筛选题一", "筛选题二")
    first = api["task"](phase_id, problem_ids, deadline_at="2099-11-20")
    second = api["task"](phase_id, problem_ids, deadline_at="2099-11-21")
    for task_id in (first, second):
        api["assign"](task_id, "指定会员", member_ids=[api["ids"]["student01"]])
    client = api["client"]
    listing = client.get("/training-tasks?page_size=1&page=2").json()["data"]
    assert listing["total"] == 2
    assert [(item["id"], item["problem_count"], item["assignee_count"]) for item in listing["items"]] == [
        (first, 2, 1),
    ]
    listing = client.get("/training-tasks?deadline_from=2099-11-21&deadline_to=2099-11-21").json()["data"]
    assert [item["id"] for item in listing["items"]] == [second]

    api["checkin"]("student01", first, checkin_date="2026-11-20")
    api["checkin"]("student01", first, checkin_date="2026-11-21")
    api["checkin"]("student01", second, checkin_date="2026-11-21")
    api["member_csrf"]("coach0001")
    params = {"task_id": first, "member_id": api["ids"]["student01"], "checkin_date": "2026-11-21"}
    records = client.get("/checkins", params=params).json()["data"]
    assert records["total"] == 1
    assert [(item["task_id"], item["checkin_date"]) for item in records["items"]] == [(first, "2026-11-21")]
    records = client.get("/checkins", params=params | {"member_id": api["ids"]["student02"]}).json()["data"]
    assert records["items"] == []
