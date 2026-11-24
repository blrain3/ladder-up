from app.db import SessionLocal
from app.models import Member
from app.models.base import local_time
from app.security import hash_password
from app.training import assignee_ids, visible_to_member


def test_publish_freezes_membership_at_publish_time(training_api):
    api = training_api
    plan_id = api["plan"]()
    phase_id = api["phase"](plan_id)
    problem_id = api["problems"]("A+B")[0]
    task_id = api["task"](phase_id, [problem_id])
    api["assign"](task_id, "全体会员")

    with SessionLocal(bind=api["engine"]) as session, session.begin():
        member = Member(
            student_no="late0001", name="后来会员", role="member", dept_id=None,
            member_type="正式会员", member_status="正式会员", password_hash=hash_password("FixturePassword!"),
            created_at=local_time(), updated_at=local_time(),
        )
        session.add(member)
        session.flush()
        new_member_id = member.id

    with SessionLocal(bind=api["engine"]) as session:
        targets = assignee_ids(session, task_id)
        assert ("会员", new_member_id) not in targets
        assert visible_to_member(session, task_id, api["ids"]["student01"]) is True
        assert visible_to_member(session, task_id, new_member_id) is False
    api["member_csrf"]("late0001")
    assert api["client"].get(f"/training-tasks/{task_id}").status_code == 404


def test_publish_twice_conflicts(training_api):
    api = training_api
    plan_id = api["plan"]()
    phase_id = api["phase"](plan_id)
    problem_id = api["problems"]("A+B")[0]
    task_id = api["task"](phase_id, [problem_id])
    api["assign"](task_id, "指定会员", member_ids=[api["ids"]["student01"]])
    response = api["assign"](task_id, "指定会员", expect=400)
    assert response.json()["code"] == 40002


def test_assign_only_before_publish(training_api):
    api = training_api
    client = api["client"]
    plan_id = api["plan"]()
    phase_id = api["phase"](plan_id)
    problem_id = api["problems"]("A+B")[0]
    task_id = api["task"](phase_id, [problem_id], member_ids=[api["ids"]["student02"]])
    api["assign"](task_id, "指定会员")

    response = api["assign"](task_id, "指定会员", member_ids=[api["ids"]["student01"]], expect=400)
    assert response.json()["code"] == 40002

    api["member_csrf"]("student02")
    assert client.get(f"/training-tasks/{task_id}").status_code == 200
    api["member_csrf"]("student01")
    assert client.get(f"/training-tasks/{task_id}").status_code == 404


def test_task_patch_and_reject_team_enum(training_api):
    api = training_api
    client = api["client"]
    headers = {"X-CSRF-Token": api["coach_csrf"]}
    phase_id = api["phase"](api["plan"]())
    problem_ids = api["problems"]("题包")
    response = client.post("/training-tasks", headers=headers, json={
        "phase_id": phase_id, "title": "旧标题", "problem_ids": problem_ids, "assignee_type": "指定队伍",
    })
    assert (response.status_code, response.json()["code"]) == (400, 40001)
    task_id = api["task"](phase_id, problem_ids, member_ids=[api["ids"]["student01"]])
    url = f"/training-tasks/{task_id}"
    assert api["assign"](task_id, "指定队伍", team_ids=[1], expect=400).json()["code"] == 40001
    assert client.patch(url, headers=headers, json={"title": "草稿标题"}).json()["data"]["title"] == "草稿标题"
    assert api["assign"](task_id, "指定会员").json()["data"]["status"] == "已下发"
    updated = client.patch(url, headers=headers, json={"title": "下发标题", "deadline_at": "2026-12-31"})
    assert updated.json()["data"]["deadline_at"] == "2026-12-31 23:59:59"
    denied = client.patch(url, headers=headers, json={"assignee_type": "全体会员"})
    assert (denied.status_code, denied.json()["code"]) == (400, 40001)
    data = client.get(url).json()["data"]
    assert data["title"] == "下发标题" and data["status"] == "已下发"
    assert [row["id"] for row in data["assignees"]] == [api["ids"]["student01"]]


def test_term_and_inclusive_phase_boundaries(training_api):
    api = training_api
    client = api["client"]
    headers = {"X-CSRF-Token": api["coach_csrf"]}
    for term in ("xxx", "2026-2027-3", "2027-2026-1", "2026-2028-2", "1999-winter", "2101-summer", "0000-0001-1"):
        response = client.post("/training-plans", headers=headers, json={"term": term})
        assert (response.status_code, response.json()["code"]) == (400, 40001)

    winter = api["plan"]("2028-winter")
    api["phase"](winter, name="前段", start_on="2028-01-01", end_on="2028-02-27")

    def phase(start, end):
        return client.post("/training-phases", headers=headers, json={
            "plan_id": winter, "name": "边界阶段", "start_on": start, "end_on": end,
        })

    overlap = phase("2028-02-27", "2028-02-29")
    assert overlap.status_code == 400 and overlap.json()["code"] == 40002
    assert phase("2028-02-28", "2028-02-29").status_code == 200
    assert phase("2028-03-01", "2028-03-02").status_code == 400
    summer = api["plan"]("2100-summer")
    assert api["phase"](summer, start_on="2100-07-01", end_on="2100-08-31") > 0
