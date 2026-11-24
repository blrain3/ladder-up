import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import make_engine


@pytest.fixture
def database_url(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'app.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("SESSION_TTL_HOURS", "8")
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    return url


@pytest.fixture
def engine(database_url):
    engine = make_engine(database_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def accounts(engine):
    from app.db import SessionLocal
    from app.models import Department, Member
    from app.models.base import local_time
    from app.security import hash_password

    password = "FixturePassword!"
    password_hash = hash_password(password)
    now = local_time()
    with SessionLocal(bind=engine) as session, session.begin():
        technical = Department(name="技术部", type="技术", created_at=now, updated_at=now)
        publicity = Department(name="宣传部", type="宣传", created_at=now, updated_at=now)
        session.add_all([technical, publicity])
        session.flush()
        accounts = {}
        for student_no, role, department in (
            ("student01", "member", technical),
            ("student02", "member", publicity),
            ("deptadmin", "dept_admin", technical),
        ):
            member = Member(
                student_no=student_no, name=f"会员 {student_no}", role=role, dept_id=department.id,
                member_type="正式会员", member_status="正式会员", password_hash=password_hash,
                created_at=now, updated_at=now,
            )
            session.add(member)
            session.flush()
            accounts[student_no] = member.id
    return accounts, password


@pytest.fixture
def client_factory(database_url):
    from contextlib import ExitStack

    from fastapi.testclient import TestClient

    from app.main import app

    with ExitStack() as stack:
        def make_client():
            return stack.enter_context(TestClient(
                app, base_url="https://testserver", follow_redirects=False,
                backend_options={"use_uvloop": sys.platform != "win32"},
            ))

        yield make_client


@pytest.fixture
def client(client_factory):
    return client_factory()


@pytest.fixture
def signed_in(accounts):
    def login(client, student_no="student01"):
        _, password = accounts
        response = client.post("/auth/login", json={"student_no": student_no, "password": password})
        assert response.status_code == 303
        return client.get("/me").json()["data"]["csrf_token"]

    return login


@pytest.fixture
def application_form(client):
    import re

    def load():
        response = client.get("/apply")
        assert response.status_code == 200
        token = re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)
        assert token == client.cookies["csrf_anon"]
        return {"student_no": "apply001", "name": "申请人", "csrf_token": token}

    return load
