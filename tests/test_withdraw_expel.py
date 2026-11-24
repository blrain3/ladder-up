import json

from sqlalchemy import select

from app.db import SessionLocal
from app.models import AuditLog, Member, MemberApplication, MemberSession


def test_leave_request_keeps_sessions_then_approval_revokes_all(
    client, client_factory, accounts, signed_in, engine, admin,
):
    ids, _ = accounts
    member_id = ids["student01"]
    path = f"/members/{member_id}/leave"
    csrf = signed_in(client)
    another = client_factory()
    signed_in(another)
    headers = {"X-CSRF-Token": csrf}
    assert client.post(f"/members/{member_id}/withdraw", json={}, headers=headers).status_code == 403
    response = client.post(path, json={"reason": "个人原因退出"}, headers=headers)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "待审核"
    before = client.get("/me")
    assert before.status_code == 200
    status_before = before.json()["data"]["member_status"]
    assert status_before not in {"已退会", "已除名"}
    duplicate = client.post(path, json={}, headers=headers)
    assert duplicate.status_code == 409 and duplicate.json()["code"] == 40901
    with SessionLocal(bind=engine) as session:
        application = session.get(MemberApplication, data["application_id"])
        assert (application.apply_type, application.active_flag, application.status) == ("退会", "1", "待审核")
        assert session.get(Member, member_id).member_status == status_before

    reviewer = client_factory()
    review_csrf = signed_in(reviewer, "admin0001")
    response = reviewer.post(f"{path}/approve", json={}, headers={"X-CSRF-Token": review_csrf})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["member_status"] == "已退会"
    for browser in (client, another):
        revoked = browser.get("/me")
        assert revoked.status_code == 401 and revoked.json()["code"] == 40101
    with SessionLocal(bind=engine) as session:
        member = session.get(Member, member_id)
        assert member.member_status == "已退会" and member.left_on
        application = session.get(MemberApplication, data["application_id"])
        assert application.status == "已通过" and application.active_flag is None
        sessions = session.scalars(select(MemberSession).where(MemberSession.member_id == member_id)).all()
        assert len(sessions) >= 2 and all(row.revoked_at for row in sessions)
        audit = session.scalar(select(AuditLog).where(AuditLog.action == "member.leave.approved"))
        assert audit.target_id == member_id


def test_leave_rejection_keeps_member_active(client, client_factory, accounts, signed_in, admin, engine):
    ids, _ = accounts
    member_id = ids["student01"]
    path = f"/members/{member_id}/leave"
    csrf = signed_in(client)
    status_before = client.get("/me").json()["data"]["member_status"]
    first = client.post(path, json={}, headers={"X-CSRF-Token": csrf}).json()["data"]
    reviewer = client_factory()
    review_csrf = signed_in(reviewer, "admin0001")
    response = reviewer.post(f"{path}/approve", json={"decision": "reject"}, headers={"X-CSRF-Token": review_csrf})
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "已拒绝"
    assert client.get("/me").json()["data"]["member_status"] == status_before
    with SessionLocal(bind=engine) as session:
        application = session.get(MemberApplication, first["application_id"])
        assert application.status == "已拒绝" and application.active_flag is None
    assert client.post(path, json={}, headers={"X-CSRF-Token": csrf}).status_code == 200


def test_admin_withdraw_and_duplicate_withdraw_returns_40002(client, accounts, signed_in, admin):
    ids, _ = accounts
    admin_csrf = signed_in(client, "admin0001")
    response = client.post(
        f"/members/{ids['student02']}/withdraw",
        json={"reason": "管理员代办退会"},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert response.status_code == 200, response.text

    duplicate = client.post(
        f"/members/{ids['student02']}/withdraw", json={},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert duplicate.status_code == 400
    assert duplicate.json()["code"] == 40002


def test_expel_requires_reason(client, accounts, signed_in, admin):
    ids, _ = accounts
    admin_csrf = signed_in(client, "admin0001")
    response = client.post(
        f"/members/{ids['student02']}/expel", json={"reason": "太短"},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert response.status_code == 400
    assert response.json()["code"] == 40001

    with SessionLocal(bind=client.app.state.engine) as session:
        assert session.get(Member, ids["student02"]).member_status == "正式会员"


def test_expel_success_writes_reason_audit_and_revokes_sessions(client, client_factory,
                                                                accounts, signed_in, admin, engine):
    ids, _ = accounts
    signed_in(client, "student02")
    old_sid = client.cookies["sid"]
    admin_csrf = signed_in(client, "admin0001")
    response = client.post(
        f"/members/{ids['student02']}/expel",
        json={"reason": "违反协会纪律，多次缺勤且拒不改正"},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["member_status"] == "已除名"

    with SessionLocal(bind=engine) as session:
        member = session.get(Member, ids["student02"])
        assert member.member_status == "已除名"
        audit = session.scalar(select(AuditLog).where(AuditLog.action == "member.expel"))
        assert audit is not None
        detail = json.loads(audit.after_json)
        assert "违反协会纪律" in detail["reason"]
        sessions = session.scalars(select(MemberSession).where(
            MemberSession.member_id == member.id
        )).all()
        assert sessions and all(row.revoked_at is not None for row in sessions)

    fresh = client_factory()
    revoked = fresh.get("/me", headers={"Cookie": f"sid={old_sid}"})
    assert revoked.status_code == 401
    assert revoked.json()["code"] == 40101
