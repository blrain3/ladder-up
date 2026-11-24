from sqlalchemy import select

from app.db import SessionLocal
from app.models import AuditLog, MemberSession
from app.security import token_digest


def test_login_success_sets_a_secure_cookie(client, accounts, signed_in, engine):
    ids, _ = accounts
    signed_in(client)
    token = client.cookies["sid"]
    cookie = next(cookie for cookie in client.cookies.jar if cookie.name == "sid")
    assert cookie.secure and cookie.path == "/"
    assert cookie.has_nonstandard_attr("HttpOnly")
    assert cookie.get_nonstandard_attr("SameSite") == "lax"
    assert client.get("/").headers["location"] == "/members"
    with SessionLocal(bind=engine) as session:
        assert session.scalar(select(MemberSession.token_hash)) == token_digest(token)
        audit = session.scalar(select(AuditLog).where(AuditLog.action == "auth.login.success"))
        assert audit.request_id
        assert audit.actor_id == audit.target_id == ids["student01"]


def test_wrong_password_returns_40101_and_creates_audit(client, accounts, engine):
    response = client.post("/auth/login", json={"student_no": "student01", "password": "wrong"})
    assert response.status_code == 401
    assert response.json() == {"code": 40101, "message": "学号或密码错误", "data": None}
    assert "sid" not in response.cookies
    with SessionLocal(bind=engine) as session:
        audit = session.scalar(select(AuditLog).where(AuditLog.action == "auth.login.failed"))
        assert audit.request_id == response.headers["X-Request-ID"]
        assert audit.actor_id is None
        assert audit.target_id == accounts[0]["student01"]


def test_browser_login_failure_renders_error(client, accounts):
    response = client.post(
        "/auth/login", data={"student_no": "student01", "password": "wrong"},
        headers={"Accept": "text/html"},
    )
    assert response.status_code == 401
    assert 'data-error-code="40101"' in response.text
    assert '<form method="post" action="/auth/login">' in response.text


def test_logout_revokes_cookie(client, signed_in, engine):
    csrf = signed_in(client)
    cookie = client.cookies["sid"]
    response = client.post("/auth/logout", json={}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 204
    assert "sid" not in client.cookies
    response = client.get("/members", headers={"Cookie": f"sid={cookie}"})
    assert response.status_code == 401
    assert response.json()["code"] == 40101
    with SessionLocal(bind=engine) as session:
        assert session.scalar(select(AuditLog.action).where(AuditLog.action == "auth.logout"))
