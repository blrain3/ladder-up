import csv
import io
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from sqlalchemy import event, func, insert, select

from app.models import Checkin, Department
from app.models.base import local_time


def _published_task(api, *, deadline_at=None, problem_count=1, member_ids=None):
    plan_id = api["plan"]()
    phase_id = api["phase"](plan_id)
    problem_ids = api["problems"](*(f"P{i}" for i in range(problem_count)))
    task_id = api["task"](phase_id, problem_ids, deadline_at=deadline_at)
    if member_ids is None:
        member_ids = [api["ids"]["student01"]]
    api["assign"](task_id, "指定会员", member_ids=member_ids)
    return task_id, problem_ids


def test_duplicate_checkin_conflicts_with_existing_id(training_api):
    api = training_api
    task_id, _ = _published_task(api)
    first = api["checkin"]("student01", task_id, solved_count=0)
    first_id = first.json()["data"]["id"]
    second = api["checkin"]("student01", task_id, solved_count=0, expect=409)
    assert second.json()["code"] == 40901
    assert second.json()["data"]["existingCheckinId"] == first_id


def test_checkin_after_deadline_rejected(training_api):
    api = training_api
    task_id, _ = _published_task(api, deadline_at="2020-01-01")
    rejected = api["checkin"]("student01", task_id, solved_count=0, expect=400)
    assert rejected.json()["code"] == 40002
    assert "截止" in rejected.json()["message"]


def test_coach_with_approve_cannot_checkin_after_deadline(training_api, monkeypatch):
    from app.auth import ROLE_PERMISSIONS

    api = training_api
    monkeypatch.setitem(ROLE_PERMISSIONS, "coach", ROLE_PERMISSIONS["coach"] | {"checkin:write:self"})
    task_id, problem_ids = _published_task(
        api, deadline_at="2020-01-01", member_ids=[api["ids"]["student01"], api["coach_id"]],
    )
    rejected = api["checkin"](
        "coach0001", task_id, solved_count=1, solved_problem_ids=[problem_ids[0]], expect=400,
    )
    assert rejected.json()["code"] == 40002


def test_solved_count_mismatch_40001(training_api):
    api = training_api
    task_id, problem_ids = _published_task(api, problem_count=2)
    response = api["checkin"](
        "student01", task_id, solved_count=1,
        problems_payload=[
            {"problem_id": problem_ids[0], "solved": 1, "attempts": None},
            {"problem_id": problem_ids[1], "solved": 1, "attempts": None},
        ],
        expect=400,
    )
    assert response.json()["code"] == 40001


def test_zero_solved_with_zero_detail_allowed(training_api):
    api = training_api
    task_id, _ = _published_task(api, problem_count=2)
    response = api["checkin"]("student01", task_id, solved_count=0)
    assert response.status_code == 200
    assert response.json()["data"]["solved_count"] == 0


def test_checkin_validates_problem_membership_and_deduplicates(training_api):
    api = training_api
    task_id, [inside] = _published_task(api)
    [outside] = api["problems"]("包外题")
    for problem_id in (outside, 99999):
        response = api["checkin"]("student01", task_id, solved_count=1,
                                  solved_problem_ids=[problem_id], expect=400)
        assert response.json()["code"] == 40001
        csrf = api["member_csrf"]("student01")
        form = api["client"].post("/checkins", data={
            "csrf_token": csrf, "task_id": task_id, "solved_count": 1, "solved_problem_ids": problem_id,
        })
        assert form.status_code == 400 and form.json()["code"] == 40001

    response = api["checkin"]("student01", task_id, solved_count=1, solved_problem_ids=[inside, inside])
    checkins = api["client"].get(f"/checkins?task_id={task_id}").json()["data"]["items"]
    [checkin] = checkins
    assert checkin["id"] == response.json()["data"]["id"]
    assert [row["problem_id"] for row in checkin["problems"]] == [inside]


def test_checkin_permissions_and_future_date(training_api):
    api = training_api
    task_id, _ = _published_task(api, member_ids=[api["ids"]["student01"], api["coach_id"]])
    assert api["checkin"]("coach0001", task_id, expect=403).json()["code"] == 40301
    response = api["checkin"]("student01", task_id, checkin_date="2100-01-01", expect=400)
    assert response.json()["code"] == 40001
    csrf = api["member_csrf"]("student01")
    response = api["client"].post("/checkins", headers={"X-CSRF-Token": csrf}, json={
        "task_id": task_id, "member_id": api["coach_id"], "solved_count": 0,
    })
    assert response.status_code == 403


def test_checkin_includes_final_second_of_date_deadline(training_api, monkeypatch):
    from datetime import datetime

    from app import routes

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 24, 23, 59, 59, 999999, tzinfo=tz)

    monkeypatch.setattr(routes, "datetime", FixedDateTime)
    api = training_api
    task_id, _ = _published_task(api, deadline_at="2026-11-24")
    assert api["checkin"]("student01", task_id).json()["code"] == 0


def test_concurrent_same_key_returns_one_existing_id(training_api, client_factory, signed_in):
    api = training_api
    task_id, _ = _published_task(api)
    clients = [api["client"], client_factory()]
    tokens = [signed_in(client) for client in clients]
    barrier = Barrier(2)
    connections = set()

    def observe(connection, _cursor, statement, *_args):
        if statement == "BEGIN IMMEDIATE":
            connections.add(id(connection.connection.driver_connection))
            barrier.wait(timeout=5)

    engines = {client.app.state.engine for client in clients}
    for engine in engines:
        event.listen(engine, "before_cursor_execute", observe)

    def submit(index):
        return clients[index].post("/checkins", json={"task_id": task_id, "solved_count": 0},
                                   headers={"X-CSRF-Token": tokens[index]})

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = sorted(pool.map(submit, range(2)), key=lambda response: response.status_code)
    finally:
        for engine in engines:
            event.remove(engine, "before_cursor_execute", observe)

    created, duplicate = results
    assert len(connections) == 2
    assert (created.status_code, duplicate.status_code) == (200, 409)
    assert duplicate.json()["code"] == 40901
    assert duplicate.json()["data"]["existingCheckinId"] == created.json()["data"]["id"]
    with api["engine"].connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Checkin)) == 1


@pytest.mark.parametrize("timeout", [5000, 100])
def test_checkin_during_100_row_csv_import(training_api, timeout):
    api = training_api
    task_id, _ = _published_task(api)
    csrf = api["member_csrf"]("student01")
    app_engine = api["client"].app.state.engine
    reached_write = Event()
    now = local_time()

    def observe(connection, _cursor, statement, *_args):
        if statement == "BEGIN IMMEDIATE":
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000
            if timeout == 100:
                connection.exec_driver_sql("PRAGMA busy_timeout=100")
            reached_write.set()

    def checkin():
        return api["client"].post("/checkins", json={"task_id": task_id, "solved_count": 0},
                                  headers={"X-CSRF-Token": csrf})

    csv_data = "name\n" + "".join(f"训练导入部门{index}\n" for index in range(100))
    rows = [dict(row, type="其他", created_at=now, updated_at=now)
            for row in csv.DictReader(io.StringIO(csv_data))]
    event.listen(app_engine, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with api["engine"].begin() as connection:
                assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
                connection.execute(insert(Department), rows)
                pending = pool.submit(checkin)
                assert reached_write.wait(5)
                if timeout == 100:
                    assert pending.result(timeout=2).json()["code"] == 50301
                else:
                    with pytest.raises(TimeoutError):
                        pending.result(timeout=0.2)
            response = pending.result(timeout=5)
    finally:
        event.remove(app_engine, "before_cursor_execute", observe)

    assert response.status_code == (503 if timeout == 100 else 200)
    with api["engine"].connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Department)
                                 .where(Department.name.like("训练导入部门%"))) == 100
        assert connection.scalar(select(func.count()).select_from(Checkin)) == (0 if timeout == 100 else 1)
