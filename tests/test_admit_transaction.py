import pytest
import sqlalchemy.exc
from sqlalchemy import select

import app.routes as routes
from app.db import SessionLocal
from app.models import Member, MemberApplication
from app.models.base import local_time


def login(client, student_no, password):
    response = client.post("/auth/login", json={"student_no": student_no, "password": password})
    assert response.status_code == 303
    return client.get("/me").json()["data"]["csrf_token"]


def create_interviewed_application(engine):
    with SessionLocal(bind=engine) as session, session.begin():
        application = MemberApplication(
            student_no="202400000099", name="申请人", apply_type="入会",
            status="已面试", active_flag="1", interviewed_at=local_time(), created_at=local_time(),
        )
        session.add(application)
        session.flush()
        return application.id


def test_admit_rolls_back_when_member_creation_fails(client, admin, engine, monkeypatch):
    application_id = create_interviewed_application(engine)
    csrf = login(client, admin["student_no"], admin["password"])

    def simulated_member_failure(*_args, **_kwargs):
        raise sqlalchemy.exc.IntegrityError("INSERT", {}, Exception("simulated failure"))

    monkeypatch.setattr(routes, "ensure_member_for_application", simulated_member_failure)
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        client.post(
            f"/admin/applications/{application_id}/admit",
            json={}, headers={"X-CSRF-Token": csrf},
        )

    with SessionLocal(bind=engine) as session:
        application = session.get(MemberApplication, application_id)
        assert application.status == "已面试"
        assert application.active_flag == "1"
        assert application.member_id is None
        assert session.scalar(select(Member).where(Member.student_no == "202400000099")) is None
