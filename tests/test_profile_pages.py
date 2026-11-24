from app.db import SessionLocal
from app.models import Member, MemberAssessment, MemberAward
from app.models.base import local_time


def _seed_profile_page_data(engine, member_id):
    now = local_time()
    with SessionLocal(bind=engine) as session, session.begin():
        session.add(MemberAssessment(
            member_id=member_id, term="2026-2027-1", result="通过", in_watchlist=0,
            assessed_at="2026-11-24 10:00:00", remark="学期考核",
        ))
        session.add(MemberAward(
            member_id=member_id, award_type="奖励", title="优秀会员", reason="表现突出",
            happened_on="2026-11-20", extra_points=150, created_at=now,
        ))


def test_profile_page_html_contains_member_and_award_points(client, accounts, coach, engine, signed_in):
    member_id = accounts[0]["student01"]
    _seed_profile_page_data(engine, member_id)
    signed_in(client, "coach0001")
    response = client.get(f"/members/{member_id}/profile", headers={"Accept": "text/html"})
    assert response.status_code == 200, response.text
    assert response.headers["Content-Type"].startswith("text/html")
    html = response.text
    assert "student01" in html
    assert "1.50" in html
    assert "优秀会员" in html
    assert "训练" in html and "考核" in html and "赛事" in html


def test_stats_page_html_contains_counts(client, accounts, coach, engine, signed_in):
    with SessionLocal(bind=engine) as session, session.begin():
        student = session.get(Member, accounts[0]["student01"])
        student.grade = 2024
        student.major = "计科"
    signed_in(client, "coach0001")
    response = client.get("/stats/members", headers={"Accept": "text/html"})
    assert response.status_code == 200, response.text
    assert response.headers["Content-Type"].startswith("text/html")
    html = response.text
    assert "统计范围" in html and "全协会" in html
    assert "<dt>在册人数</dt><dd>4</dd>" in html
    for title, expected in (
        ("年级", [("2024", 1)]),
        ("专业", [("计科", 1)]),
        ("部门", [("技术部", 3), ("宣传部", 1)]),
        ("会员类型", [("正式会员", 3), ("实验室成员", 1)]),
    ):
        table = html.split(f"<h2>{title}分布</h2>", 1)[1].split("</table>", 1)[0]
        for value, count in expected:
            assert f"<tr><td>{value}</td><td>{count}</td></tr>" in table
    assert "<tr><td>技术部</td><td>正式会员</td><td>2</td></tr>" in html



def test_awards_page_html_contains_award_rows(client, accounts, coach, signed_in):
    member_id = accounts[0]["student01"]
    csrf = signed_in(client, "coach0001")
    created = client.post(
        "/awards",
        json={
            "member_id": member_id, "award_type": "奖励", "title": "优秀会员",
            "happened_on": "2026-11-20", "extra_points": 150,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 200, created.text

    response = client.get("/awards", headers={"Accept": "text/html"})
    assert response.status_code == 200, response.text
    assert response.headers["Content-Type"].startswith("text/html")
    html = response.text
    assert "奖惩记录" in html
    assert "student01" in html and "1.50" in html and "优秀会员" in html


def test_stats_navigation_follows_granted_read_permission(client, accounts, signed_in, grant_permission):
    signed_in(client, "student01")
    headers = {"Accept": "text/html"}
    before = client.get("/members", headers=headers)
    assert 'href="/stats/members"' not in before.text
    assert 'href="/members/export"' not in before.text
    assert 'href="/awards?scope=mine"' in before.text
    assert client.get("/stats/members").status_code == 403

    grant_permission(accounts[0]["student01"], "member:read")
    after = client.get("/members", headers=headers).text
    assert 'href="/stats/members"' in after
    assert 'href="/awards?scope=mine"' in after
    assert client.get("/stats/members").status_code == 200


def test_mine_awards_only_includes_current_member(client, accounts, coach, signed_in, grant_permission):
    csrf = signed_in(client, "coach0001")
    for student, title in (("student01", "本人奖励"), ("student02", "他人奖励")):
        response = client.post(
            "/awards",
            json={
                "member_id": accounts[0][student], "award_type": "奖励", "title": title,
                "happened_on": "2026-11-20", "extra_points": 150,
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200, response.text

    signed_in(client, "student01")
    for broad_read in (False, True):
        if broad_read:
            grant_permission(accounts[0]["student01"], "member:read")
        html = client.get("/awards?scope=mine", headers={"Accept": "text/html"})
        assert html.status_code == 200
        assert "本人奖励" in html.text
        assert "他人奖励" not in html.text
        data = client.get("/awards?scope=mine").json()["data"]
        assert data["total"] == 1
        assert [item["student_no"] for item in data["items"]] == ["student01"]
    assert client.get("/awards").json()["data"]["total"] == 2

    signed_in(client, "coach0001")
    html = client.get("/awards?scope=mine", headers={"Accept": "text/html"})
    assert html.status_code == 200
    assert 'href="/awards"' in html.text
    assert 'action="/awards"' not in html.text
    assert "本人奖励" not in html.text and "他人奖励" not in html.text
