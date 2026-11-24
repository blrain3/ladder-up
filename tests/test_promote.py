import json

import pytest
from sqlalchemy import select

from app.assessment import promote_ready
from app.db import SessionLocal
from app.models import AuditLog, Member, SysConfig


def _setup_training(api, phase_id, problem_ids, member_id, solved):
    task_id = api["task"](phase_id, problem_ids, title=f"训练{len(problem_ids)}题")
    api["assign"](task_id, "指定会员", member_ids=[member_id])
    api["checkin"]("student01", task_id, solved_count=solved,
                   solved_problem_ids=problem_ids[:solved])
    return task_id


def _pass_assessment(api, score=80):
    test_id = api["create_test"]()
    api["record"](test_id, [{"member_id": api["ids"]["student01"],
                             "solved_count": 4, "score": score}])
    return api["run_assess"]()


def test_promote_ready_with_pass_and_completion_06(assessment_api):
    api = assessment_api
    phase_id = api["phase"](api["plan"]())
    problem_ids = api["problems"]("甲", "乙", "丙", "丁", "戊")
    _setup_training(api, phase_id, problem_ids, api["ids"]["student01"], solved=3)
    _pass_assessment(api)

    with SessionLocal(bind=api["engine"]) as session:
        ready, reasons = promote_ready(session, api["ids"]["student01"], "2026-2027-1")
    assert ready is True
    assert reasons == []

    with SessionLocal(bind=api["engine"]) as session, session.begin():
        session.get(SysConfig, "training.promote_threshold").config_value = "0.7"
    csrf = api["member_csrf"]("admin0001")
    response = api["client"].post(
        f"/members/{api['ids']['student01']}/promote", json={}, headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400 and response.json()["code"] == 40002


def test_promote_not_ready_when_completion_below_threshold(assessment_api):
    api = assessment_api
    phase_id = api["phase"](api["plan"]())
    problem_ids = api["problems"]("子", "丑")
    _setup_training(api, phase_id, problem_ids, api["ids"]["student01"], solved=1)
    _pass_assessment(api)

    with SessionLocal(bind=api["engine"]) as session:
        ready, reasons = promote_ready(session, api["ids"]["student01"], "2026-2027-1")
    assert ready is False
    assert any("训练完成率" in reason for reason in reasons)


def test_promote_not_ready_when_assessment_not_passed(assessment_api):
    api = assessment_api
    phase_id = api["phase"](api["plan"]())
    problem_ids = api["problems"]("寅", "卯")
    _setup_training(api, phase_id, problem_ids, api["ids"]["student01"], solved=2)
    _pass_assessment(api, score=50)

    with SessionLocal(bind=api["engine"]) as session:
        ready, reasons = promote_ready(session, api["ids"]["student01"], "2026-2027-1")
    assert ready is False
    assert any("考核" in reason for reason in reasons)


def test_promote_updates_status_type_and_writes_audit(assessment_api):
    api = assessment_api
    phase_id = api["phase"](api["plan"]())
    problem_ids = api["problems"]("辰", "巳", "午", "未", "申")
    _setup_training(api, phase_id, problem_ids, api["ids"]["student01"], solved=3)
    _pass_assessment(api)
    admin_csrf = api["member_csrf"]("admin0001")

    response = api["client"].post(
        f"/members/{api['ids']['student01']}/promote", json={},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["member_status"] == "正式会员"

    with SessionLocal(bind=api["engine"]) as session:
        member = session.get(Member, api["ids"]["student01"])
        assert member.member_status == "正式会员"
        assert member.member_type == "正式会员"
        audit = session.scalar(select(AuditLog).where(AuditLog.action == "member.promote"))
        assert audit is not None
        assert audit.target_id == member.id
        assert json.loads(audit.after_json)["term"] == "2026-2027-1"

@pytest.mark.parametrize(("solved", "expected"), [(1, False), (2, False), (4, True), (None, False)])
def test_completion_matrix(assessment_api, solved, expected):
    api = assessment_api
    member_id = api["ids"]["student01"]
    phase_id = api["phase"](api["plan"]())
    problems = api["problems"]("甲", "乙", "丙", "丁")
    if solved is not None:
        _setup_training(api, phase_id, problems, member_id, solved)
    else:
        task = api["task"](phase_id, problems)
        api["assign"](task, "指定会员", member_ids=[api["ids"]["student02"]])
    _pass_assessment(api)
    with SessionLocal(bind=api["engine"]) as session:
        ready, reasons = promote_ready(session, member_id, "2026-2027-1")
    assert ready is expected
    if solved is None:
        assert any("无已下发训练任务" in reason for reason in reasons)


def test_historical_term_is_read_only_and_invalid_states_are_rejected(assessment_api):
    api = assessment_api
    target = api["ids"]["student01"]
    other = api["ids"]["student02"]
    csrf = api["member_csrf"]("admin0001")
    headers = {"X-CSRF-Token": csrf}
    historical = api["client"].get("/promotions?term=2025-2026-1").json()["data"]["items"]
    assert historical[0]["id"] == target
    assert not historical[0]["ready"] and "历史学期只读" in historical[0]["reasons"]
    for member_id, payload, status, code in [
        (target, {"term": "2025-2026-1"}, 400, 40002),
        (other, {}, 400, 40002),
        (999999, {}, 404, 40401),
    ]:
        response = api["client"].post(f"/members/{member_id}/promote", json=payload, headers=headers)
        assert response.status_code == status and response.json()["code"] == code
    with SessionLocal(bind=api["engine"]) as session, session.begin():
        session.get(Member, target).deleted_at = "2026-11-24"
    response = api["client"].post(f"/members/{target}/promote", json={}, headers=headers)
    assert response.status_code == 400 and response.json()["code"] == 40002


def test_department_approver_can_act_only_on_own_department(assessment_api):
    from app.models import MemberPermission, PermissionPoint
    from app.models.base import local_time

    api = assessment_api
    client, ids = api["client"], api["ids"]
    own, other = ids["student01"], ids["student02"]
    phase_id = api["phase"](api["plan"]())
    _setup_training(api, phase_id, api["problems"]("甲"), own, solved=1)
    test_id = api["create_test"]()
    api["record"](test_id, [{"member_id": own, "score": 80}])
    api["run_assess"]()
    with SessionLocal(bind=api["engine"]) as session, session.begin():
        session.get(Member, other).member_status = "观察会员"
        for code in ("training:approve", "member:approve"):
            point = session.scalar(select(PermissionPoint).where(PermissionPoint.code == code))
            if point is None:
                point = PermissionPoint(code=code, module="A", description=code)
                session.add(point)
                session.flush()
            session.add(MemberPermission(
                member_id=ids["deptadmin"], permission_id=point.id, granted_at=local_time(),
            ))

    csrf = api["member_csrf"]("deptadmin")
    headers = {"X-CSRF-Token": csrf}
    scores_path = f"/assessment-tests/{test_id}/scores"
    scores = client.get(scores_path).json()["data"]
    own_score = next(row for row in scores["items"] if row["id"] == own)
    assert {row["student_no"] for row in scores["items"]} == {
        "admin0001", "coach0001", "deptadmin", "student01",
    }
    assert client.get(f"{scores_path}?page_size=1").json()["data"]["total"] == 4
    assert len(client.get(f"{scores_path}?page_size=1&page=2").json()["data"]["items"]) == 1
    page = client.get(scores_path, headers={"Accept": "text/html"})
    assert "student01" in page.text and "student02" not in page.text
    assert client.post("/assessment-tests", json={
        "title": "部门不能建全局周测", "test_type": "周测", "held_on": "2026-11-20",
    }, headers=headers).status_code == 403
    foreign = client.post(scores_path, json={
        "rows": [{"member_id": other, "version": 0, "score": 80}],
    }, headers=headers)
    assert foreign.status_code == 404 and foreign.json()["code"] == 40401
    local = client.post(scores_path, json={
        "rows": [{"member_id": own, "version": own_score["version"], "score": 85}],
    }, headers=headers)
    assert local.json()["data"]["updated"] == 1
    assert [row["id"] for row in client.get("/promotions").json()["data"]["items"]] == [own]
    for action, data in (("promote", {}), ("expel", {"reason": "会员多次违反协会管理规定"})):
        forbidden = client.post(f"/members/{other}/{action}", json=data, headers=headers)
        assert forbidden.status_code == 404 and forbidden.json()["code"] == 40401
        allowed = client.post(f"/members/{own}/{action}", json=data, headers=headers)
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["data"]["member_status"] == ("正式会员" if action == "promote" else "已除名")
