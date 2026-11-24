from sqlalchemy import select

from app.db import SessionLocal
from app.models import AuditLog, MemberSession
from app.security import token_digest


def test_password_change_rotates_and_revokes_sessions(client, client_factory, accounts, signed_in, engine):
    _, old_password = accounts
    other = client_factory()
    csrf = signed_in(client)
    signed_in(other)
    old_sid = client.cookies["sid"]
    other_sid = other.cookies["sid"]
    assert old_sid != other_sid
    new_password = "ReplacementPassword!"
    response = client.put(
        "/me/password", json={"old_password": old_password, "new_password": new_password},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 204
    new_sid = client.cookies["sid"]
    assert new_sid != old_sid
    assert client.get("/me").json()["data"]["csrf_token"] != csrf
    for cookie in (old_sid, other_sid):
        response = client.get("/members", headers={"Cookie": f"sid={cookie}"})
        assert response.status_code == 401
        assert response.json()["code"] == 40101
    with SessionLocal(bind=engine) as session:
        rows = {row.token_hash: row for row in session.scalars(select(MemberSession))}
        assert rows[token_digest(old_sid)].revoke_reason == "password_change"
        assert rows[token_digest(other_sid)].revoked_at
        assert rows[token_digest(new_sid)].revoked_at is None
        assert session.scalar(select(AuditLog.id).where(AuditLog.action == "auth.password.changed"))
    for password, expected in ((old_password, 401), (new_password, 303)):
        response = other.post("/auth/login", json={"student_no": "student01", "password": password})
        assert response.status_code == expected
