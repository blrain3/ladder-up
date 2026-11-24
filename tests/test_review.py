import json
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import AuditLog, Member, MemberApplication
from app.models.base import local_time


def login(client, student_no, password):
    response = client.post("/auth/login", json={"student_no": student_no, "password": password})
    assert response.status_code == 303
    return client.get("/me").json()["data"]["csrf_token"]


def create_application(engine, status="待审核", student_no="202400000099"):
    active_flag = "1" if status in ("待审核", "待面试", "已面试") else None
    with SessionLocal(bind=engine) as session, session.begin():
        application = MemberApplication(
            student_no=student_no, name="申请人", major="软件工程", grade=2024,
            apply_type="入会", status=status, active_flag=active_flag, created_at=local_time(),
        )
        session.add(application)
        session.flush()
        return application.id


def assert_audit(session, response, action, actor_id, target_id):
    event = session.scalars(select(AuditLog).where(
        AuditLog.request_id == response.headers["X-Request-ID"],
    )).one()
    assert (event.action, event.actor_id, event.target_type, event.target_id) == (
        action, actor_id, "application", target_id,
    )
    return json.loads(event.after_json) if event.after_json else None


def test_review_interview_admit_flow(client, admin, engine):
    application_id = create_application(engine, "待审核")
    csrf = login(client, admin["student_no"], admin["password"])

    review = client.post(
        f"/admin/applications/{application_id}/review",
        json={"result": "通过", "review_note": "材料齐全"}, headers={"X-CSRF-Token": csrf},
    )
    assert review.status_code == 200
    assert review.json()["data"]["status"] == "待面试"

    interview = client.post(
        f"/admin/applications/{application_id}/interview",
        json={"result": "通过", "interview_note": "面试表现良好"}, headers={"X-CSRF-Token": csrf},
    )
    assert interview.status_code == 200
    assert interview.json()["data"]["status"] == "已面试"

    admit = client.post(
        f"/admin/applications/{application_id}/admit",
        json={}, headers={"X-CSRF-Token": csrf},
    )
    assert admit.status_code == 200
    assert "录取成功" in admit.text

    with SessionLocal(bind=engine) as session:
        application = session.get(MemberApplication, application_id)
        assert application.status == "已通过"
        assert application.active_flag is None
        assert application.interviewed_at
        member = session.get(Member, application.member_id)
        assert f'data-member-id="{member.id}"' in admit.text
        assert member.student_no == application.student_no
        assert member.member_status == "观察会员"
        assert member.member_type == "观察会员"
        assert member.must_change_password == 1
        assert member.joined_on == local_time()[:10]
        assert_audit(session, review, "application.review", admin["id"], application_id)
        assert_audit(session, interview, "application.interview", admin["id"], application_id)
        assert_audit(session, admit, "application.admit", admin["id"], application_id)


def test_reject_path_clears_active_flag(client, admin, engine):
    application_id = create_application(engine, "待审核")
    csrf = login(client, admin["student_no"], admin["password"])
    response = client.post(
        f"/admin/applications/{application_id}/review",
        json={"result": "拒绝", "review_note": "材料不符合要求"}, headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "已拒绝"
    with SessionLocal(bind=engine) as session:
        application = session.get(MemberApplication, application_id)
        assert application.active_flag is None
        assert application.reviewed_at
        detail = assert_audit(session, response, "application.review", admin["id"], application_id)
        assert detail["result"] == "拒绝"


def test_member_without_approve_permission_gets_403(client, signed_in, engine, accounts):
    application_id = create_application(engine, "待审核")
    csrf = signed_in(client)
    response = client.post(
        f"/admin/applications/{application_id}/review",
        json={"result": "通过", "review_note": "越权尝试"}, headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 403
    assert response.json()["code"] == 40301
    with SessionLocal(bind=engine) as session:
        assert_audit(session, response, "auth.permission.denied", accounts[0]["student01"], application_id)


def test_application_list_renders_rows_and_review_action(client, admin, engine):
    application_id = create_application(engine)
    login(client, admin["student_no"], admin["password"])
    response = client.get("/admin/applications", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert f"<td>{application_id}</td>" in response.text
    assert f'action="/admin/applications/{application_id}/review"' in response.text
    assert ">通过</button>" in response.text
    for value in ("202400000099", "申请人", "待审核"):
        assert f"<td>{value}</td>" in response.text


def test_read_permission_can_list_applications_without_approval_controls(client, signed_in, engine):
    application_id = create_application(engine)
    signed_in(client, "deptadmin")
    response = client.get("/admin/applications", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert f"<td>{application_id}</td>" in response.text
    assert ">通过</button>" not in response.text


def test_failed_interview_records_date_reason_and_audit(client, admin, engine):
    application_id = create_application(engine, "待面试")
    csrf = login(client, admin["student_no"], admin["password"])
    response = client.post(
        f"/admin/applications/{application_id}/interview",
        json={"result": "不通过", "interview_note": "未达到要求"}, headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    with SessionLocal(bind=engine) as session:
        application = session.get(MemberApplication, application_id)
        assert application.status == "已拒绝" and application.active_flag is None
        assert application.interviewed_at and application.interview_note == "未达到要求"
        detail = assert_audit(session, response, "application.interview", admin["id"], application_id)
        assert detail["result"] == "不通过"


@pytest.mark.parametrize(("status", "deleted", "message"), [
    ("已退会", False, "需先由管理员恢复会员"),
    ("已除名", False, "需先由管理员恢复会员"),
    ("正式会员", False, "会员已存在"),
    ("观察会员", False, "会员已存在"),
    ("正式会员", True, "需先由管理员恢复会员"),
])
def test_admit_rejects_existing_member_without_changes(client, admin, engine, status, deleted, message):
    application_id = create_application(engine, "已面试", "202400000088")
    csrf = login(client, admin["student_no"], admin["password"])
    with SessionLocal(bind=engine) as session, session.begin():
        existing = session.scalar(select(Member).where(Member.student_no == "student01"))
        existing.student_no = "202400000088"
        existing.member_status = status
        existing.left_on = "2025-01-02"
        existing.deleted_at = local_time() if deleted else None
        existing_id = existing.id
        before = {column.name: getattr(existing, column.name) for column in Member.__table__.columns}

    response = client.post(
        f"/admin/applications/{application_id}/admit", json={}, headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert response.json()["code"] == 40002
    assert response.json()["message"] == message
    with SessionLocal(bind=engine) as session:
        existing = session.get(Member, existing_id)
        assert {key: getattr(existing, key) for key in before} == before
        application = session.get(MemberApplication, application_id)
        assert (application.status, application.active_flag, application.member_id) == (
            "已面试", "1", None,
        )


def test_first_login_after_admission_requires_password_change(client, client_factory, admin, engine):
    application_id = create_application(engine, "已面试")
    other_id = create_application(engine, "已面试", "202400000098")
    csrf = login(client, admin["student_no"], admin["password"])

    def admit(id):
        response = client.post(
            f"/admin/applications/{id}/admit", data={"csrf_token": csrf},
            headers={"Accept": "text/html"},
        )
        assert response.status_code == 200
        password = re.search(r'id="one-time-password">([^<]+)', response.text).group(1)
        assert len(password) == 16
        assert response.headers["Cache-Control"] == "no-store"
        with SessionLocal(bind=engine) as session:
            detail = assert_audit(session, response, "application.admit", admin["id"], id)
            assert detail["one_time_password_issued"] is True
            assert password not in json.dumps(detail)
        return password

    password = admit(application_id)
    assert admit(other_id) != password
    assert password not in client.get("/admin/applications", headers={"Accept": "text/html"}).text
    new_member = client_factory()
    credentials = {"student_no": "202400000099", "password": password}
    response = new_member.post("/auth/login", json=credentials)
    assert response.status_code == 303 and response.headers["location"] == "/me/password"
    for method, path in [("GET", "/members"), ("GET", "/apply"), ("POST", "/apply")]:
        response = new_member.request(method, path)
        assert response.status_code == 303 and response.headers["location"] == "/me/password"
    response = new_member.get("/me/password")
    assert response.status_code == 200
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)
    response = new_member.post(
        "/me/password", json={"old_password": password, "new_password": "NewFixturePassword!"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 204
    response = new_member.get("/members", headers={"Accept": "text/html"})
    assert response.status_code == 200 and "202400000099" in response.text
    with SessionLocal(bind=engine) as session:
        application = session.get(MemberApplication, application_id)
        assert session.get(Member, application.member_id).must_change_password == 0
    assert new_member.post("/auth/login", json=credentials).status_code == 401


def test_concurrent_reviews_only_one_succeeds(client, client_factory, admin, engine):
    application_id = create_application(engine)
    other = client_factory()
    pairs = [(c, login(c, admin["student_no"], admin["password"])) for c in (client, other)]
    barrier = Barrier(2)

    def review(pair, result):
        c, csrf = pair
        barrier.wait(timeout=5)
        return c.post(
            f"/admin/applications/{application_id}/review",
            json={"result": result, "review_note": result}, headers={"X-CSRF-Token": csrf},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        tasks = [executor.submit(review, pair, result) for pair, result in zip(pairs, ("拒绝", "通过"), strict=True)]
        responses = [task.result(timeout=10) for task in tasks]
    assert sorted(response.json()["code"] for response in responses) in ([0, 40002], [0, 40901])
    winner = next(response for response in responses if response.status_code == 200)
    with SessionLocal(bind=engine) as session:
        application = session.get(MemberApplication, application_id)
        assert application.status == winner.json()["data"]["status"]
        assert application.active_flag == ("1" if application.status == "待面试" else None)
        events = session.scalars(select(AuditLog).where(AuditLog.action == "application.review")).all()
        assert len(events) == 1


@pytest.mark.parametrize("invalid_id", ["9" * 100, "0", "-1", "abc", "1.0"])
def test_invalid_object_ids_return_40001(client, admin, engine, invalid_id):
    csrf = login(client, admin["student_no"], admin["password"])
    for response in (
        client.get(f"/members/{invalid_id}"),
        client.get(f"/files/{invalid_id}"),
        client.post(
            f"/admin/applications/{invalid_id}/review",
            json={"result": "通过", "review_note": "材料齐全"}, headers={"X-CSRF-Token": csrf},
        ),
    ):
        assert response.status_code == 400
        assert response.json()["code"] == 40001


def test_out_of_range_department_id_returns_40001(client, admin, engine):
    application_id = create_application(engine, "已面试")
    csrf = login(client, admin["student_no"], admin["password"])
    response = client.post(
        f"/admin/applications/{application_id}/admit",
        json={"dept_id": str(2**63)}, headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert response.json()["code"] == 40001
