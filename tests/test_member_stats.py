from sqlalchemy import select

from app.db import SessionLocal
from app.models import Department, Member
from app.models.base import local_time
from app.profile import member_stats


def _seed_stats_data(engine, accounts):
    now = local_time()
    technical_id = None
    with SessionLocal(bind=engine) as session, session.begin():
        technical = session.scalar(select(Department).where(Department.name == "技术部"))
        publicity = session.scalar(select(Department).where(Department.name == "宣传部"))
        session.add(Department(name="空部门", type="其他", created_at=now, updated_at=now))
        technical_id = technical.id

        student01 = session.get(Member, accounts[0]["student01"])
        student01.grade = 2024
        student01.major = "计科"
        student01.joined_on = "2026-09-01"

        student02 = session.get(Member, accounts[0]["student02"])
        student02.grade = 2025
        student02.major = "软工"
        student02.joined_on = "2026-09-02"

        session.add(Member(
            student_no="left0001", name="已退会", role="member", dept_id=technical.id,
            member_type="观察会员", member_status="已退会", grade=2023, major="网安",
            joined_on="2026-09-03", left_on="2026-11-10",
            password_hash=student01.password_hash, created_at=now, updated_at=now,
        ))
        session.add(Member(
            student_no="gone0001", name="已软删", role="member", dept_id=technical.id,
            member_type="正式会员", member_status="正式会员", grade=2022, major="人工智能",
            joined_on="2026-09-04", deleted_at=now,
            password_hash=student01.password_hash, created_at=now, updated_at=now,
        ))
        session.add(Member(
            student_no="left0002", name="已除名", role="member", dept_id=publicity.id,
            member_type="正式会员", member_status="已除名", grade=2021, major="大数据",
            joined_on="2026-09-05", left_on="2026-11-11",
            password_hash=student01.password_hash, created_at=now, updated_at=now,
        ))
    return technical_id


def _values(rows):
    return {row["value"]: row["count"] for row in rows}


def test_member_stats_keeps_zero_value_items_and_active_scope(engine, accounts):
    _seed_stats_data(engine, accounts)
    with SessionLocal(bind=engine) as session:
        stats = member_stats(session)

    assert stats["active"] == 3
    assert stats["new_this_term"] == 4
    assert stats["left_this_term"] == 2

    grades = _values(stats["distributions"]["grade"])
    assert grades[2022] == 0
    assert grades[2023] == 0
    assert grades[2024] == 1
    assert grades[2025] == 1

    majors = _values(stats["distributions"]["major"])
    assert majors["人工智能"] == 0
    assert majors["网安"] == 0
    assert majors["计科"] == 1
    assert majors["软工"] == 1

    departments = _values(stats["distributions"]["department"])
    assert departments["空部门"] == 0
    assert departments["技术部"] == 2
    assert departments["宣传部"] == 1

    member_types = _values(stats["distributions"]["member_type"])
    assert member_types["观察会员"] == 0
    assert member_types["正式会员"] == 3


def test_member_stats_empty_database_does_not_crash(engine):
    with SessionLocal(bind=engine) as session:
        stats = member_stats(session)
    assert stats["active"] == 0
    assert stats["new_this_term"] == 0
    assert stats["left_this_term"] == 0
    for rows in stats["distributions"].values():
        assert rows == []


def test_dept_admin_stats_only_count_own_department(client, accounts, engine, signed_in):
    _seed_stats_data(engine, accounts)
    signed_in(client, "deptadmin")
    response = client.get("/stats/members")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["scope"] == "本部门"
    assert data["active"] == 2
    departments = _values(data["distributions"]["department"])
    assert set(departments) == {"技术部"}
    assert departments["技术部"] == 2


def test_stats_only_soft_deleted_members_keep_zero_counts(engine):
    now = local_time()
    with SessionLocal(bind=engine) as session, session.begin():
        used = Department(name="历史部门", type="技术", created_at=now, updated_at=now)
        unused = Department(name="未使用部门", type="其他", created_at=now, updated_at=now)
        session.add_all([used, unused])
        session.flush()
        session.add(Member(
            student_no="gone0001", name="已软删", role="member", dept_id=used.id,
            member_type="正式会员", member_status="正式会员", grade=2024, major="计科",
            joined_on="2026-09-01", deleted_at=now, password_hash="unused", created_at=now, updated_at=now,
        ))
    with SessionLocal(bind=engine) as session:
        stats = member_stats(session, "2026-2027-1")
    assert (stats["active"], stats["new_this_term"], stats["left_this_term"]) == (0, 0, 0)
    distributions = stats["distributions"]
    assert _values(distributions["department"]) == {"历史部门": 0, "未使用部门": 0}
    assert _values(distributions["member_type"]) == {"正式会员": 0}
    assert _values(distributions["grade"]) == {2024: 0}
    assert _values(distributions["major"]) == {"计科": 0}
    assert distributions["department_member_type"] == [
        {"department": name, "member_type": "正式会员", "count": 0} for name in ("历史部门", "未使用部门")
    ]


def test_association_stats_http_includes_unused_department(client, accounts, coach, engine, signed_in):
    _seed_stats_data(engine, accounts)
    signed_in(client, coach["student_no"])
    response = client.get("/stats/members?term=2026-2027-1")
    assert response.status_code == 200
    stats = response.json()["data"]
    distributions = stats["distributions"]
    assert _values(distributions["department"])["空部门"] == 0
    combinations = distributions["department_member_type"]
    assert sum(item["count"] for item in combinations) == stats["active"]
    assert {"department": "空部门", "member_type": "正式会员", "count": 0} in combinations
