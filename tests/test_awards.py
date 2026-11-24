from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from zoneinfo import ZoneInfo

from sqlalchemy import event, select

from app.db import SessionLocal
from app.models import Member, MemberAward
from app.models.base import local_time

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _coach_csrf(client, signed_in):
    return signed_in(client, "coach0001")


def test_create_award_stores_integer_basis_points(client, accounts, coach, signed_in):
    csrf = _coach_csrf(client, signed_in)
    member_id = accounts[0]["student01"]
    response = client.post(
        "/awards",
        json={
            "member_id": member_id, "award_type": "奖励", "title": "优秀会员",
            "happened_on": "2026-11-20", "extra_points": 150,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["extra_points"] == 150
    assert data["points"] == "1.50"

    html = client.get("/awards", headers={"Accept": "text/html"})
    assert html.status_code == 200
    assert "1.50" in html.text


def test_duplicate_award_same_day_title_returns_40901(client, accounts, coach, signed_in):
    csrf = _coach_csrf(client, signed_in)
    member_id = accounts[0]["student01"]
    payload = {
        "member_id": member_id, "award_type": "奖励", "title": "优秀会员",
        "happened_on": "2026-11-20", "extra_points": 150,
    }
    first = client.post("/awards", json=payload, headers={"X-CSRF-Token": csrf})
    assert first.status_code == 200, first.text
    duplicate = client.post("/awards", json=payload, headers={"X-CSRF-Token": csrf})
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == 40901
    assert duplicate.json()["data"]["id"] == first.json()["data"]["id"]


def test_award_happened_on_cannot_be_in_future(client, accounts, coach, signed_in):
    csrf = _coach_csrf(client, signed_in)
    member_id = accounts[0]["student01"]
    future = (datetime.now(SHANGHAI).date() + timedelta(days=1)).isoformat()
    response = client.post(
        "/awards",
        json={
            "member_id": member_id, "award_type": "惩处", "title": "警告",
            "happened_on": future, "extra_points": -50,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert response.json()["code"] == 40001


def test_member_role_cannot_create_award(client, accounts, coach, signed_in):
    csrf = signed_in(client, "student01")
    member_id = accounts[0]["student01"]
    response = client.post(
        "/awards",
        json={
            "member_id": member_id, "award_type": "奖励", "title": "优秀会员",
            "happened_on": "2026-11-20", "extra_points": 150,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 403
    assert response.json()["code"] == 40301


def test_award_summary_sums_positive_and_negative(client, accounts, coach, signed_in):
    csrf = _coach_csrf(client, signed_in)
    member_id = accounts[0]["student01"]
    for title, day, points in (("优秀会员", "2026-11-20", 150), ("警告", "2026-11-21", -50)):
        response = client.post(
            "/awards",
            json={
                "member_id": member_id, "award_type": "奖励" if points > 0 else "惩处",
                "title": title, "happened_on": day, "extra_points": points,
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200, response.text

    signed_in(client, "student01")
    profile = client.get(f"/members/{member_id}/profile").json()["data"]
    assert profile["award_summary"] == 100
    assert profile["award_summary_points"] == "1.00"


def test_award_creation_looks_up_targets_inside_department(client, accounts, engine, signed_in, grant_permission):
    ids = accounts[0]
    grant_permission(ids["deptadmin"], "member:approve")
    csrf = signed_in(client, "deptadmin")

    def post(member_id):
        return client.post(
            "/awards",
            json={"member_id": member_id, "award_type": "奖励", "title": "部门奖励", "happened_on": "2026-11-20"},
            headers={"X-CSRF-Token": csrf},
        )

    cross_department = post(ids["student02"])
    unknown = post(987654)
    with SessionLocal(bind=engine) as session, session.begin():
        session.get(Member, ids["student02"]).deleted_at = local_time()
    cross_department_deleted = post(ids["student02"])
    assert cross_department.status_code == unknown.status_code == cross_department_deleted.status_code == 404
    assert cross_department.json() == unknown.json() == cross_department_deleted.json()

    assert post(ids["student01"]).status_code == 200
    with SessionLocal(bind=engine) as session, session.begin():
        session.get(Member, ids["student01"]).deleted_at = local_time()
    deleted = post(ids["student01"])
    assert (deleted.status_code, deleted.json()["code"]) == (400, 40002)


def test_concurrent_awards_on_independent_connections_only_one_succeeds(
    client_factory, accounts, coach, engine, signed_in,
):
    clients = [client_factory(), client_factory()]
    pairs = [(client, signed_in(client, coach["student_no"])) for client in clients]
    barrier = Barrier(2, timeout=10)
    writer_connections = set()
    application_engine = clients[0].app.state.engine

    def synchronize_writes(connection, cursor, statement, parameters, context, executemany):
        if statement == "BEGIN IMMEDIATE":
            writer_connections.add(id(connection.connection.driver_connection))
            barrier.wait()

    payload = {
        "member_id": accounts[0]["student01"], "award_type": "奖励",
        "title": "并发奖励", "happened_on": "2026-11-20",
    }

    def post(pair):
        client, csrf = pair
        return client.post("/awards", json=payload, headers={"X-CSRF-Token": csrf})

    event.listen(application_engine, "before_cursor_execute", synchronize_writes)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            accepted, duplicate = sorted(executor.map(post, pairs), key=lambda response: response.status_code)
    finally:
        event.remove(application_engine, "before_cursor_execute", synchronize_writes)
    assert len(writer_connections) == 2
    assert (accepted.status_code, duplicate.status_code) == (200, 409)
    assert duplicate.json()["code"] == 40901
    with SessionLocal(bind=engine) as session:
        award = session.scalars(select(MemberAward)).one()
        assert award.id == accepted.json()["data"]["id"] == duplicate.json()["data"]["id"]


def test_award_pagination_counts_only_visible_members(client, accounts, engine, signed_in):
    ids = accounts[0]
    with SessionLocal(bind=engine) as session, session.begin():
        session.add_all(MemberAward(
            member_id=member_id, award_type="奖励", title=title,
            happened_on="2026-11-20", created_at=local_time(),
        ) for title, member_id in (
            ("同部门较早", ids["student01"]), ("同部门较晚", ids["student01"]), ("跨部门", ids["student02"]),
        ))
    signed_in(client, "deptadmin")
    pages = [client.get(f"/awards?page={page}&page_size=1").json()["data"] for page in (1, 2)]
    assert [page["total"] for page in pages] == [2, 2]
    assert [page["items"][0]["title"] for page in pages] == ["同部门较晚", "同部门较早"]
