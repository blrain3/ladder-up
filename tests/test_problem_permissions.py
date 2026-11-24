from sqlalchemy import select

from app.db import SessionLocal
from app.models import AuditLog


def test_anonymous_problems_401(client):
    response = client.get("/problems")
    assert response.status_code == 401
    assert response.json()["code"] == 40101


def test_member_can_read_but_not_write(client, signed_in, accounts, engine):
    csrf = signed_in(client)
    assert client.get("/problems").status_code == 200
    response = client.post(
        "/problems",
        json={"title": "越权题目", "difficulty": "入门", "problem_type": "程序", "kp_ids": [1]},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 403
    assert response.json()["code"] == 40301
    with SessionLocal(bind=engine) as session:
        denial = session.scalars(select(AuditLog).where(
            AuditLog.action == "auth.permission.denied"
        )).one()
        assert (denial.actor_id, denial.action, denial.target_type) == (
            accounts[0]["student01"], "auth.permission.denied", "problem",
        )


def test_coach_can_write(client, signed_in, coach):
    csrf = signed_in(client, "coach0001")
    root = client.post(
        "/knowledge-points", json={"name": "图论"}, headers={"X-CSRF-Token": csrf}
    ).json()["data"]
    response = client.post(
        "/problems",
        json={
            "title": "A+B", "difficulty": "入门", "problem_type": "程序",
            "source": "自建", "kp_ids": [root["id"]],
            "sample_in": "1 2", "sample_out": "3",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert response.json()["data"]["title"] == "A+B"


def test_dept_admin_cannot_write(client, signed_in):
    csrf = signed_in(client, "deptadmin")
    assert client.get("/problems").status_code == 200
    response = client.post(
        "/problems",
        json={"title": "越权题目", "difficulty": "入门", "problem_type": "程序", "kp_ids": [1]},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 403
    assert response.json()["code"] == 40301
