import re

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Member, MemberApplication
from app.models.base import local_time


def test_public_application_creates_only_an_application(client, application_form, engine):
    form = application_form()
    response = client.post("/apply", data=form)
    assert response.status_code == 200
    assert "查询回执码" in response.text
    with SessionLocal(bind=engine) as session:
        application = session.scalars(select(MemberApplication)).one()
        assert application.student_no == form["student_no"]
        assert application.apply_type == "入会"
        assert application.status == "待审核"
        assert application.active_flag == "1"
        assert session.scalar(select(Member)) is None


def test_duplicate_active_application_returns_conflict(client, application_form, engine):
    form = application_form()
    assert client.post("/apply", data=form).status_code == 200
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
    assert client.post("/apply", data=form).status_code == 200
    with SessionLocal(bind=engine) as session:
        assert session.scalars(select(MemberApplication.status).order_by(MemberApplication.id)).all() == [
            "已拒绝", "已撤回", "待审核",
        ]


def test_invalid_student_no_is_rejected(client, application_form):
    form = application_form() | {"student_no": "abc12345"}
    response = client.post("/apply", data=form)
    assert response.status_code == 400
    assert response.json()["code"] == 40001


def test_missing_consent_is_rejected(client, application_form):
    form = application_form()
    form.pop("agree_observation")
    response = client.post("/apply", data=form)
    assert response.status_code == 400
    assert response.json()["code"] == 40001


def test_mismatched_csrf_token_is_rejected(client, application_form):
    form = application_form() | {"csrf_token": "mismatched"}
    response = client.post("/apply", data=form)
    assert response.status_code == 400
    assert response.json()["code"] == 40002


def test_status_lookup_requires_receipt(client, application_form, engine):
    from app.security import token_digest

    form = application_form()
    submission = client.post("/apply", data=form)
    receipt = re.search(r'id="receipt-code">([^<]+)', submission.text).group(1)
    with SessionLocal(bind=engine) as session:
        application = session.scalars(select(MemberApplication)).one()
        assert application.receipt_code_hash == token_digest(receipt)
        old_params = {"application_no": application.id, "student_no": application.student_no}

    assert "待审核" not in client.get("/apply/status").text
    assert client.get("/apply/status", params=old_params).status_code == 404
    assert client.post("/apply/status", data={}).status_code == 404
    assert client.post("/apply/status", data={"receipt_code": "z" * 22}).status_code == 404
    result = client.post("/apply/status", data={"receipt_code": receipt})
    assert result.status_code == 200
    assert "待审核" in result.text
    assert receipt not in result.text


@pytest.mark.parametrize(("field", "value"), [
    ("student_no", "1" * 5000), ("grade", "9" * 5000),
    ("grade", "1999"), ("grade", "2101"), ("phone", "1" * 12),
])
def test_application_input_limits(client, application_form, field, value):
    response = client.post("/apply", data=application_form() | {field: value})
    assert response.status_code == 400
    assert response.json()["code"] == 40001
