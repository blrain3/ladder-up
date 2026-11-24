from sqlalchemy import select

from app.db import SessionLocal
from app.models import Member, MemberApplication
from app.models.base import local_time


def test_public_application_creates_only_an_application(client, application_form, engine):
    form = application_form()
    response = client.post("/apply", data=form)
    assert response.status_code == 303
    assert client.get(response.headers["location"]).status_code == 200
    with SessionLocal(bind=engine) as session:
        application = session.scalars(select(MemberApplication)).one()
        assert application.student_no == form["student_no"]
        assert application.apply_type == "入会"
        assert application.status == "待审核"
        assert application.active_flag == "1"
        assert session.scalar(select(Member)) is None


def test_duplicate_active_application_returns_conflict(client, application_form, engine):
    form = application_form()
    assert client.post("/apply", data=form).status_code == 303
    response = client.post("/apply", data=form)
    assert response.status_code == 409
    assert response.json()["code"] == 40901
    with SessionLocal(bind=engine) as session:
        assert response.json()["data"]["id"] == session.scalars(select(MemberApplication.id)).one()


def test_terminal_applications_do_not_prevent_a_new_application(client, application_form, engine):
    form = application_form()
    with SessionLocal(bind=engine) as session, session.begin():
        for status in ("已拒绝", "已撤回"):
            session.add(MemberApplication(
                student_no=form["student_no"], name=form["name"], apply_type="入会",
                status=status, active_flag=None, created_at=local_time(),
            ))
    assert client.post("/apply", data=form).status_code == 303
    with SessionLocal(bind=engine) as session:
        assert session.scalars(select(MemberApplication.status).order_by(MemberApplication.id)).all() == [
            "已拒绝", "已撤回", "待审核",
        ]
