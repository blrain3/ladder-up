import csv
import io
from datetime import datetime
from zoneinfo import ZoneInfo

from app.db import SessionLocal
from app.models import AssessmentTest, Member, MemberAssessment, MemberAward, TestScore
from app.models.base import local_time
from app.training import active_members_query

SHANGHAI = ZoneInfo("Asia/Shanghai")
BOM = b"\xef\xbb\xbf"


def _seed_export_data(engine, member_id):
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


def _rows(content):
    text = content.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


def _section(rows, title):
    index = rows.index([title])
    assert rows[index + 1]  # header follows the section title
    return index


def test_export_csv_contract(client, accounts, engine, signed_in):
    member_id = accounts[0]["student01"]
    _seed_export_data(engine, member_id)
    signed_in(client, "student01")

    response = client.get(f"/members/{member_id}/export?format=csv")
    assert response.status_code == 200, response.text
    assert response.headers["Content-Type"].startswith("text/csv")
    today = datetime.now(SHANGHAI).strftime("%Y%m%d")
    assert response.headers["Content-Disposition"] == (
        f'attachment; filename="profile-student01-{today}.csv"'
    )
    assert response.content.startswith(BOM)
    assert b"\r\n" in response.content
    assert b"\n" not in response.content.replace(b"\r\n", b"")

    rows = _rows(response.content)
    basic = _section(rows, "[基本信息]")
    training = _section(rows, "[训练]")
    assessment = _section(rows, "[考核]")
    awards = _section(rows, "[奖惩]")
    contest = _section(rows, "[赛事]")
    assert basic < training < assessment < awards < contest

    assert rows[basic + 1] == ["学号", "姓名", "部门", "会员类型", "会员状态",
                               "入会日期", "离会日期", "邮箱", "手机"]
    assert rows[basic + 2][0] == "student01"

    assert rows[training + 1] == ["任务", "完成率", "打卡次数", "最后打卡日期"]
    assert rows[training + 2][0] == "0"

    assert rows[assessment + 1] == ["学期", "结果", "考核时间", "备注"]
    assert rows[assessment + 2][0] == "2026-2027-1"

    assert rows[awards + 1] == ["日期", "类型", "标题", "分值(分)", "事由"]
    assert rows[awards + 2][3] == "1.50"
    assert rows[awards + 2][2] == "优秀会员"

    assert rows[contest + 1] == ["日期", "赛事", "队伍", "名次", "奖项"]
    assert rows[contest + 2] == ["无记录"]


def test_export_csv_keeps_contest_header_when_no_data(client, accounts, signed_in):
    member_id = accounts[0]["student01"]
    signed_in(client, "student01")
    response = client.get(f"/members/{member_id}/export?format=csv")
    rows = _rows(response.content)
    contest = _section(rows, "[赛事]")
    assert rows[contest + 1] == ["日期", "赛事", "队伍", "名次", "奖项"]
    assert rows[contest + 2] == ["无记录"]


def test_export_rejects_non_csv_format(client, accounts, signed_in):
    member_id = accounts[0]["student01"]
    signed_in(client, "student01")
    response = client.get(f"/members/{member_id}/export?format=xlsx")
    assert response.status_code == 400
    assert response.json()["code"] == 40001


def test_full_members_export_is_admin_only(client, accounts, signed_in):
    signed_in(client, "student01")
    response = client.get("/members/export")
    assert response.status_code == 403
    assert response.json()["code"] == 40301


def test_export_preserves_signed_points_and_escapes_untrusted_text(client, accounts, admin, engine, signed_in):
    member_id = accounts[0]["student01"]
    name = "=cmd|' /C calc'!A0"
    phone = "13812345678"
    with SessionLocal(bind=engine) as session, session.begin():
        member = session.get(Member, member_id)
        member.name = name
        member.phone = phone
        member.email = " \tordinary@example.com"
        session.add(MemberAward(
            member_id=member_id, award_type="惩处", title="+SUM(1)", reason="\t普通说明",
            happened_on="2026-11-20", extra_points=-50, created_at=local_time(),
        ))
        now = local_time()
        session.get(Member, accounts[0]["student02"]).deleted_at = now
        session.add_all(Member(
            student_no=f"batch{index:04d}", name=f"批量会员 {index}", role="member",
            member_type="正式会员", member_status="正式会员", dept_id=member.dept_id,
            password_hash=member.password_hash, created_at=now, updated_at=now,
        ) for index in range(100))

    signed_in(client, "student01")
    personal = client.get(f"/members/{member_id}/export")
    signed_in(client, admin["student_no"])
    full = client.get("/members/export")
    assert personal.status_code == full.status_code == 200
    assert 'href="/members/export"' in client.get("/members", headers={"Accept": "text/html"}).text

    for response in (personal, full):
        assert response.content.startswith(BOM)
        rows = _rows(response.content)
        basic = next(row for row in rows if row and row[0] == "student01")
        assert basic[1] == "'" + name
        assert basic[-2:] == ["' \tordinary@example.com", phone]
        assert ["2026-11-20", "惩处", "'+SUM(1)", "-0.50", "'\t普通说明"] in rows

    with SessionLocal(bind=engine) as session:
        active_count = len(session.scalars(active_members_query()).all())
    rows = _rows(full.content)
    sections = [row for row in rows if len(row) == 1 and row[0].startswith("[")]
    assert len(sections) == active_count * 5
    assert sections == [[title] for title in ("[基本信息]", "[训练]", "[考核]", "[奖惩]", "[赛事]")] * active_count
    assert not any(row and row[0] == "student02" for row in rows)


def test_csv_keeps_missing_and_zero_scores_distinct(client, accounts, engine, signed_in):
    member_id = accounts[0]["student01"]
    with SessionLocal(bind=engine) as session, session.begin():
        for title, score in (("未录分", None), ("零分", "0"), ("负分", "-2.5")):
            test = AssessmentTest(title=title, test_type="周测", held_on="2026-11-20", created_at=local_time())
            session.add(test)
            session.flush()
            session.add(TestScore(test_id=test.id, member_id=member_id, score=score))
        for day, title, points in (("2026-11-19", "未计分奖惩", None), ("2026-11-20", "零分奖惩", 0)):
            session.add(MemberAward(
                member_id=member_id, award_type="奖励", title=title, happened_on=day,
                extra_points=points, created_at=local_time(),
            ))

    signed_in(client, "student01")
    rows = _rows(client.get(f"/members/{member_id}/export").content)
    assert ["未录分", "2026-11-20", ""] in rows
    assert ["零分", "2026-11-20", "0.00"] in rows
    assert ["负分", "2026-11-20", "-2.50"] in rows
    assert ["测评总分", "", "-2.50"] in rows
    assert ["2026-11-19", "奖励", "未计分奖惩", "", ""] in rows
    assert ["2026-11-20", "奖励", "零分奖惩", "0.00", ""] in rows


def test_personal_csv_term_summary_includes_both_boundaries(client, accounts, engine, signed_in):
    member_id = accounts[0]["student01"]
    with SessionLocal(bind=engine) as session, session.begin():
        for day, kind, points in (
            ("2026-08-31", "奖励", 900), ("2026-09-01", "奖励", 150),
            ("2027-01-31", "惩处", -200), ("2027-02-01", "惩处", -900),
        ):
            session.add(MemberAward(
                member_id=member_id, award_type=kind, title=day, happened_on=day,
                extra_points=points, created_at=local_time(),
            ))
    signed_in(client, "student01")
    url = f"/members/{member_id}/export?format=csv"
    response = client.get(url + "&term=2026-2027-1")
    assert response.status_code == 200
    assert _rows(response.content)[-3:] == [
        ["[综测汇总]"], ["学期", "奖励合计", "惩处合计", "净分"],
        ["2026-2027-1", "1.50", "-2.00", "-0.50"],
    ]
    assert _rows(client.get(url).content)[-1] == ["未指定学期", "", "", ""]
    bad = client.get(url + "&term=invalid")
    assert (bad.status_code, bad.json()["code"]) == (400, 40001)
