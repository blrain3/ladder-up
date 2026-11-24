from sqlalchemy import select

from app.db import SessionLocal
from app.models import AuditLog, MemberPermission, PermissionPoint
from app.models.base import local_time


def test_member_reads_only_self(client, accounts, signed_in, engine):
    ids, _ = accounts
    signed_in(client)
    response = client.get(f"/members/{ids['student02']}")
    assert response.status_code == 403
    assert response.json()["code"] == 40301
    assert "student02" not in response.text
    assert client.get(f"/members/{ids['student01']}").status_code == 200
    data = client.get("/members").json()["data"]
    assert data["total"] == 1
    assert [member["id"] for member in data["items"]] == [ids["student01"]]
    with SessionLocal(bind=engine) as session:
        audit = session.scalar(select(AuditLog).where(AuditLog.action == "auth.permission.denied"))
        assert audit.actor_id == ids["student01"]
        assert audit.target_id == ids["student02"]
        assert audit.request_id == response.headers["X-Request-ID"]


def test_department_admin_lists_and_reads_only_own_department(client, accounts, signed_in):
    ids, _ = accounts
    signed_in(client, "deptadmin")
    assert client.get(f"/members/{ids['student02']}").status_code == 403
    assert client.get(f"/members/{ids['student01']}").status_code == 200
    data = client.get("/members", params={"page": 2, "size": 1}).json()["data"]
    assert (data["total"], data["page"], data["size"]) == (2, 2, 1)
    assert [member["id"] for member in data["items"]] == [ids["deptadmin"]]
    assert client.get("/members", params={"size": 101}).status_code == 400


def test_explicit_member_read_expands_scope(client, accounts, signed_in, engine):
    ids, _ = accounts
    with SessionLocal(bind=engine) as session, session.begin():
        permission = PermissionPoint(code="member:read", module="A", description="读取会员")
        session.add(permission)
        session.flush()
        session.add(MemberPermission(
            member_id=ids["student01"], permission_id=permission.id, granted_at=local_time(),
        ))
    signed_in(client)
    assert client.get(f"/members/{ids['student02']}").status_code == 200
    assert client.get("/members").json()["data"]["total"] == 3


def test_review_requires_member_approve(client, signed_in):
    csrf = signed_in(client)
    response = client.post("/member-applications/123/review", json={}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 403
    assert response.json() == {"code": 40301, "message": "无权访问此资源", "data": None}
