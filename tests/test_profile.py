import re

from app.db import SessionLocal
from app.models import Member, MemberAssessment, MemberAward
from app.models.base import local_time


def _seed_profile_history(engine, member_id):
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


def test_profile_training_matches_s4_when_two_tasks_share_a_problem(client, accounts, training_api, engine, signed_in):
    api = training_api
    member_id = accounts[0]["student01"]
    problem_ids = api["problems"]("档案题")
    plan_id = api["plan"]("2026-2027-1")
    phase_id = api["phase"](plan_id, start_on="2026-09-01", end_on="2027-01-31")
    tasks = [api["task"](phase_id, problem_ids, title=title) for title in ("档案任务一", "档案任务二")]
    for task_id in tasks:
        api["assign"](task_id, "指定会员", member_ids=[member_id])
    for task_id in tasks:
        api["checkin"]("student01", task_id, solved_count=1, solved_problem_ids=problem_ids)
    _seed_profile_history(engine, member_id)

    signed_in(client, "student01")
    response = client.get(f"/members/{member_id}/profile", headers={"Accept": "text/html"})
    assert response.status_code == 200, response.text
    assert response.headers["Content-Type"].startswith("text/html")
    html = response.text
    for section in ("基本信息", "训练", "考核", "奖惩", "赛事"):
        assert f'id="{section}"' in html or f"<h2>{section}</h2>" in html
    assert "无记录（赛事模块随 D 域交付）" in html
    assert "1.50" in html and "优秀会员" in html
    assert "<td>100.00%</td>" in html
    assert re.search(
        r'<td>2026-2027-1</td>\s*<td><span class="badge [^"]+"><i[^>]*></i>通过</span></td>',
        html,
    )
    s4 = client.get("/training-progress").json()["data"]["items"][0]
    profile = client.get(f"/members/{member_id}/profile").json()["data"]["training"]
    assert (profile["solved"], profile["total"], profile["rate"]) == (s4["solved"], s4["total"], s4["rate"])
    assert (profile["solved"], profile["total"], profile["rate"]) == (2, 2, 1.0)


def test_profile_visible_to_self_without_member_read(client, accounts, signed_in):
    member_id = accounts[0]["student01"]
    signed_in(client, "student01")
    response = client.get(f"/members/{member_id}/profile")
    assert response.status_code == 200
    assert response.json()["data"]["student_no"] == "student01"


def test_profile_of_other_requires_member_read(client, accounts, signed_in):
    signed_in(client, "student01")
    other_id = accounts[0]["student02"]
    response = client.get(f"/members/{other_id}/profile")
    assert response.status_code == 403
    assert response.json()["code"] == 40301


def test_dept_admin_cannot_view_other_department_profile(client, accounts, signed_in):
    signed_in(client, "deptadmin")
    other_id = accounts[0]["student02"]
    response = client.get(f"/members/{other_id}/profile")
    assert response.status_code == 403
    assert response.json()["code"] == 40301


def test_dept_admin_can_view_own_department_profile(client, accounts, signed_in):
    signed_in(client, "deptadmin")
    member_id = accounts[0]["student01"]
    response = client.get(f"/members/{member_id}/profile")
    assert response.status_code == 200
    assert response.json()["data"]["id"] == member_id


def test_sensitive_fields_are_consistent_across_profile_awards_and_csv(
    client, accounts, coach, engine, signed_in, grant_permission,
):
    member_id = accounts[0]["student01"]
    phone, email, reason = "13812345678", "student@example.com", "表现突出"
    with SessionLocal(bind=engine) as session, session.begin():
        target = session.get(Member, member_id)
        target.phone, target.email = phone, email
    _seed_profile_history(engine, member_id)
    grant_permission(accounts[0]["student02"], "member:read")
    grant_permission(accounts[0]["student02"], "system:admin")

    for login, expected_phone, expected_email, show_reason in (
        ("deptadmin", "138******78", "stu**************om", False),
        ("student02", None, None, False),
        ("student01", phone, email, True),
        ("coach0001", phone, email, True),
    ):
        signed_in(client, login)
        url = f"/members/{member_id}"
        profile = client.get(url + "/profile").json()["data"]
        award = client.get("/awards").json()["data"]["items"][0]
        assert profile.get("phone") == expected_phone
        assert profile.get("email") == expected_email
        assert profile["awards"][0].get("reason") == award.get("reason") == (reason if show_reason else None)
        if not show_reason:
            assert "reason" not in award and "reason" not in profile["awards"][0]
        if expected_phone is None:
            assert "phone" not in profile and "email" not in profile

        html = client.get(url + "/profile", headers={"Accept": "text/html"}).text
        csv_text = client.get(url + "/export").content.decode("utf-8-sig")
        outputs = [html, csv_text, client.get("/awards", headers={"Accept": "text/html"}).text]
        if login == "student02":
            full = client.get("/members/export")
            assert full.status_code == 200
            outputs.append(full.content.decode("utf-8-sig"))
        for output in outputs:
            assert (reason in output) == show_reason
        for output in (html, csv_text):
            assert (phone in output) == (expected_phone == phone)
            assert (email in output) == (expected_email == email)
            if expected_phone:
                assert expected_phone in output
        if login == "student02":
            assert phone not in outputs[-1] and email not in outputs[-1]
