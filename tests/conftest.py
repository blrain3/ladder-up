import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import make_engine


@pytest.fixture
def migration_head():
    return ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()


@pytest.fixture
def database_url(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'app.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv("COOKIE_SECURE", raising=False)
    monkeypatch.setenv("SESSION_TTL_HOURS", "8")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))
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
def admin(accounts, engine):
    from app.db import SessionLocal
    from app.models import Department, Member
    from app.models.base import local_time
    from app.security import hash_password

    password = "FixturePassword!"
    now = local_time()
    with SessionLocal(bind=engine) as session, session.begin():
        department = session.scalar(select(Department).where(Department.name == "技术部"))
        member = Member(
            student_no="admin0001", name="系统管理员", role="sys_admin", dept_id=department.id,
            member_type="管理会员", member_status="正式会员", password_hash=hash_password(password),
            created_at=now, updated_at=now,
        )
        session.add(member)
        session.flush()
        member_id = member.id
    return {"student_no": "admin0001", "id": member_id, "password": password}


@pytest.fixture
def coach(accounts, engine):
    from app.db import SessionLocal
    from app.models import Department, Member
    from app.models.base import local_time
    from app.security import hash_password

    password = "FixturePassword!"
    now = local_time()
    with SessionLocal(bind=engine) as session, session.begin():
        department = session.scalar(select(Department).where(Department.name == "技术部"))
        member = Member(
            student_no="coach0001", name="示例教练", role="coach", dept_id=department.id,
            member_type="实验室成员", member_status="竞赛实验室成员",
            password_hash=hash_password(password), created_at=now, updated_at=now,
        )
        session.add(member)
        session.flush()
        member_id = member.id
    return {"student_no": "coach0001", "id": member_id, "password": password}


@pytest.fixture
def application_form(client):
    import re

    def load():
        response = client.get("/apply")
        assert response.status_code == 200
        token = re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)
        assert token == client.cookies["csrf_anon"]
        return {
            "student_no": "202400000001", "name": "申请人", "agree_observation": "1",
            "csrf_token": token,
        }

    return load


@pytest.fixture
def training_api(client, signed_in, coach, accounts, engine):
    """S4 训练 API 工厂：教练侧建计划/阶段/题目/任务/指派/下发，会员侧打卡."""
    from app.db import SessionLocal
    from app.models import Problem
    from app.models.base import local_time

    coach_csrf = signed_in(client, "coach0001")
    ids, _ = accounts
    now = local_time()

    def problems(*titles):
        with SessionLocal(bind=engine) as session, session.begin():
            created = []
            for title in titles:
                problem = Problem(
                    title=title, source="自建", difficulty="入门", problem_type="程序",
                    statement=None, sample_in="1 2", sample_out="3", created_by=None,
                    created_at=now, updated_at=now,
                )
                session.add(problem)
                session.flush()
                created.append(problem.id)
        return created

    def plan(term="2026-2027-1", **extra):
        response = client.post(
            "/training-plans", json={"term": term, **extra},
            headers={"X-CSRF-Token": coach_csrf},
        )
        assert response.status_code == 200, response.text
        return response.json()["data"]["id"]

    def phase(plan_id, name="基础", start_on="2026-09-01", end_on="2027-01-31"):
        response = client.post(
            "/training-phases",
            json={"plan_id": plan_id, "name": name, "start_on": start_on, "end_on": end_on},
            headers={"X-CSRF-Token": coach_csrf},
        )
        assert response.status_code == 200, response.text
        return response.json()["data"]["id"]

    def task(phase_id, problem_ids, title="周任务", **extra):
        payload = {"phase_id": phase_id, "title": title, "problem_ids": problem_ids, **extra}
        response = client.post(
            "/training-tasks", json=payload, headers={"X-CSRF-Token": coach_csrf},
        )
        assert response.status_code == 200, response.text
        return response.json()["data"]["id"]

    def assign(task_id, assignee_type, *, member_ids=None, team_ids=None, expect=200):
        payload = {"assignee_type": assignee_type}
        if member_ids is not None:
            payload["member_ids"] = member_ids
        if team_ids is not None:
            payload["team_ids"] = team_ids
        response = client.post(
            f"/training-tasks/{task_id}/assign", json=payload,
            headers={"X-CSRF-Token": coach_csrf},
        )
        assert response.status_code == expect, response.text
        return response

    def member_csrf(student_no):
        return signed_in(client, student_no)

    def checkin(student_no, task_id, *, solved_count=0, solved_problem_ids=None,
                minutes=None, checkin_date=None, problems_payload=None, expect=200):
        csrf = signed_in(client, student_no)
        payload = {"task_id": task_id, "solved_count": solved_count}
        if minutes is not None:
            payload["minutes"] = minutes
        if checkin_date is not None:
            payload["checkin_date"] = checkin_date
        if problems_payload is not None:
            payload["problems"] = problems_payload
        elif solved_problem_ids is not None:
            payload["problems"] = [
                {"problem_id": problem_id, "solved": 1, "attempts": None}
                for problem_id in solved_problem_ids
            ]
        response = client.post("/checkins", json=payload, headers={"X-CSRF-Token": csrf})
        assert response.status_code == expect, response.text
        return response

    return {
        "client": client, "coach_csrf": coach_csrf, "ids": ids, "engine": engine,
        "coach_id": coach["id"],
        "problems": problems, "plan": plan, "phase": phase, "task": task,
        "assign": assign, "member_csrf": member_csrf, "checkin": checkin,
    }


@pytest.fixture
def assessment_api(training_api, admin, engine, monkeypatch):
    """S5 考核 API 工厂：用管理员令牌写训练与测评，跑考核同样走管理员."""
    from datetime import datetime

    from app import assessment
    from app.db import SessionLocal
    from app.models import Member, SysConfig
    from app.models.base import local_time

    class AssessmentClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 24, 12, tzinfo=tz)

    monkeypatch.setattr(assessment, "datetime", AssessmentClock)
    api = dict(training_api)
    client = api["client"]
    admin_csrf = api["member_csrf"]("admin0001")

    def current_admin_csrf():
        return api["member_csrf"]("admin0001")

    with SessionLocal(bind=engine) as session, session.begin():
        session.merge(SysConfig(
            config_key="academic_year.current", config_value="2026-2027",
            updated_at=local_time(),
        ))
        student = session.get(Member, api["ids"]["student01"])
        student.member_status = "观察会员"
        student.member_type = "观察会员"

    def plan(term="2026-2027-1", **extra):
        response = client.post(
            "/training-plans", json={"term": term, **extra},
            headers={"X-CSRF-Token": current_admin_csrf()},
        )
        assert response.status_code == 200, response.text
        return response.json()["data"]["id"]

    def phase(plan_id, name="基础", start_on="2026-09-01", end_on="2027-01-31"):
        response = client.post(
            "/training-phases",
            json={"plan_id": plan_id, "name": name, "start_on": start_on, "end_on": end_on},
            headers={"X-CSRF-Token": current_admin_csrf()},
        )
        assert response.status_code == 200, response.text
        return response.json()["data"]["id"]

    def task(phase_id, problem_ids, title="周任务", **extra):
        payload = {"phase_id": phase_id, "title": title, "problem_ids": problem_ids, **extra}
        response = client.post(
            "/training-tasks", json=payload, headers={"X-CSRF-Token": current_admin_csrf()},
        )
        assert response.status_code == 200, response.text
        return response.json()["data"]["id"]

    def assign(task_id, assignee_type, *, member_ids=None, team_ids=None, expect=200):
        payload = {"assignee_type": assignee_type}
        if member_ids is not None:
            payload["member_ids"] = member_ids
        if team_ids is not None:
            payload["team_ids"] = team_ids
        response = client.post(
            f"/training-tasks/{task_id}/assign", json=payload,
            headers={"X-CSRF-Token": current_admin_csrf()},
        )
        assert response.status_code == expect, response.text
        return response

    def create_test(title="第一周周测", test_type="周测", held_on="2026-11-20"):
        response = client.post(
            "/assessment-tests",
            json={"title": title, "test_type": test_type, "held_on": held_on},
            headers={"X-CSRF-Token": current_admin_csrf()},
        )
        assert response.status_code == 200, response.text
        return response.json()["data"]["id"]

    def record(test_id, rows):
        csrf = current_admin_csrf()
        path = f"/assessment-tests/{test_id}/scores"
        snapshot = client.get(path).json()["data"]["items"]
        versions = {row["id"]: row["version"] for row in snapshot}
        rows = [{"version": versions[row["member_id"]], **row} for row in rows]
        response = client.post(
            path, json={"rows": rows}, headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200, response.text
        return response.json()["data"]

    def run_assess(term="2026-2027-1"):
        response = client.post(
            "/assessments/run", json={"term": term},
            headers={"X-CSRF-Token": current_admin_csrf()},
        )
        assert response.status_code == 200, response.text
        return response.json()["data"]

    api.update({
        "admin_csrf": admin_csrf, "engine": engine,
        "plan": plan, "phase": phase, "task": task, "assign": assign,
        "create_test": create_test, "record": record, "run_assess": run_assess,
    })
    return api


@pytest.fixture
def grant_permission(engine):
    from app.db import SessionLocal
    from app.models import MemberPermission, PermissionPoint
    from app.models.base import local_time

    def grant(member_id, code):
        with SessionLocal(bind=engine) as session, session.begin():
            point = session.scalar(select(PermissionPoint).where(PermissionPoint.code == code))
            if point is None:
                point = PermissionPoint(code=code, module=code.split(":")[0], description=code)
                session.add(point)
                session.flush()
            session.add(MemberPermission(member_id=member_id, permission_id=point.id, granted_at=local_time()))

    return grant
