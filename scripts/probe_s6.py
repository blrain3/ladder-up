"""人工对抗性探测：S6 的 CSV 契约、权限范围、奖惩约束与统计口径。"""
import csv
import io
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/s6.db"
os.environ["APP_ENV"] = "probe"
os.environ["SESSION_TTL_HOURS"] = "8"
os.environ["UPLOADS_DIR"] = f"{tmp}/uploads"
os.environ["SEED_ADMIN_PASSWORD"] = "AdminProbe1!"
os.environ["SEED_DEMO_PASSWORD"] = "DemoProbe1!"

from alembic.config import Config

from alembic import command

command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
subprocess.run([sys.executable, "bin/seed.py"], check=True, env=os.environ, cwd=ROOT,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal, make_engine
from app.main import app
from app.models import Member
from app.models.base import local_time

TOKEN_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
results = []


def check(name, ok, detail=""):
    results.append((name, "PASS" if ok else "FAIL", detail))


def token(client, url="/me/password"):
    m = TOKEN_RE.search(client.get(url).text)
    return m.group(1) if m else ""


engine = make_engine(os.environ["DATABASE_URL"])

with SessionLocal(bind=engine) as s:
    demo1 = s.scalar(select(Member).where(Member.student_no == "demo0001"))
    demo2 = s.scalar(select(Member).where(Member.student_no == "demo0002"))
    demo1_id, demo2_id = demo1.id, demo2.id

with TestClient(
    app, headers={"Accept": "text/html"}, backend_options={"use_uvloop": sys.platform != "win32"},
) as client:
    def login(sid, pwd):
        return client.post("/auth/login", data={"student_no": sid, "password": pwd},
                           follow_redirects=False)

    check("教练登录 303", login("demo0004", "DemoProbe1!").status_code == 303)

    # 奖惩：分值 150 = 1.50 分
    r = client.post("/awards", data={"csrf_token": token(client, "/awards"),
                                     "member_id": demo1_id, "award_type": "奖励",
                                     "title": "省赛铜牌", "reason": "ICPC 省赛",
                                     "happened_on": local_time()[:10], "extra_points": "150"},
                    follow_redirects=False)
    check("录入奖励", r.status_code in (200, 303), f"got {r.status_code} {r.text[:60]}")

    dup = client.post("/awards", data={"csrf_token": token(client, "/awards"),
                                       "member_id": demo1_id, "award_type": "奖励",
                                       "title": "省赛铜牌", "reason": "重复",
                                       "happened_on": local_time()[:10], "extra_points": "150"},
                      follow_redirects=False)
    check("同日同标题重复 40901", dup.status_code == 409 and "40901" in dup.text,
          f"got {dup.status_code}")

    future = client.post("/awards", data={"csrf_token": token(client, "/awards"),
                                          "member_id": demo1_id, "award_type": "惩处",
                                          "title": "迟到", "reason": "x",
                                          "happened_on": "2099-01-01", "extra_points": "-50"},
                         follow_redirects=False)
    check("未来日期 40001", future.status_code == 400 and "40001" in future.text,
          f"got {future.status_code}")

    # 档案页
    prof = client.get(f"/members/{demo1_id}/profile")
    check("档案页 200", prof.status_code == 200, f"got {prof.status_code}")
    check("档案页含学号与分值", "demo0001" in prof.text and "1.50" in prof.text,
          f"学号={'demo0001' in prof.text} 分值={'1.50' in prof.text}")
    check("档案页赛事节标注无记录", "无记录" in prof.text or "赛事" in prof.text)

    # CSV 导出契约
    exp = client.get(f"/members/{demo1_id}/export?format=csv")
    raw = exp.content
    check("导出 200 且 text/csv", exp.status_code == 200 and "text/csv" in exp.headers.get("content-type", ""),
          f"{exp.status_code} {exp.headers.get('content-type')}")
    check("带 BOM", raw.startswith(b"\xef\xbb\xbf"), f"head={raw[:6]!r}")
    text = raw.decode("utf-8-sig")
    check("CRLF 换行", b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""))
    rows = list(csv.reader(io.StringIO(text, newline="")))
    for section in ("[基本信息]", "[训练]", "[考核]", "[奖惩]", "[赛事]"):
        check(f"含节 {section}", [section] in rows)
    flat = [c for row in rows for c in row]
    check("CSV 含奖惩标题", "省赛铜牌" in flat, f"样例={flat[:8]}")
    day = date.fromisoformat(local_time()[:10])
    first_year = day.year if day.month >= 9 else day.year - 1
    term = f"{first_year}-{first_year + 1}-{1 if day.month >= 9 or day.month == 1 else 2}"
    by_term = client.get(f"/members/{demo1_id}/export", params={"format": "csv", "term": term})
    term_rows = list(csv.reader(io.StringIO(by_term.content.decode("utf-8-sig"), newline="")))
    check("CSV 分值与综测汇总", rows[-3:] == [
        ["[综测汇总]"], ["学期", "奖励合计", "惩处合计", "净分"], ["未指定学期", "", "", ""],
    ] and by_term.status_code == 200 and term_rows[-1:] == [[term, "1.50", "0.00", "1.50"]]
        and any(row[1:4] == ["奖励", "省赛铜牌", "1.50"] for row in rows))
    check("文件名正确", "profile-demo0001-" in exp.headers.get("content-disposition", ""),
          exp.headers.get("content-disposition"))
    bad = client.get(f"/members/{demo1_id}/export?format=xlsx")
    check("非法 format 40001", bad.status_code == 400 and "40001" in bad.text, f"got {bad.status_code}")

    # 统计页
    stats = client.get("/stats/members")
    check("统计页 200", stats.status_code == 200, f"got {stats.status_code}")
    check("统计页含在册数字与组合维度", re.search(r"在册人数</dt><dd>[1-9]\d*", stats.text) is not None
          and "部门 × 会员类型" in stats.text)

    # 权限：普通会员
    client.post("/auth/logout", data={"csrf_token": token(client, "/me")}, follow_redirects=False)
    login("demo0001", "DemoProbe1!")
    own = client.get(f"/members/{demo1_id}/profile")
    other = client.get(f"/members/{demo2_id}/profile")
    check("本人看自己档案 200", own.status_code == 200, f"got {own.status_code}")
    check("看他人档案 403", other.status_code in (403, 404), f"got {other.status_code}")
    w = client.post("/awards", data={"csrf_token": token(client, "/me/password"),
                                     "member_id": demo1_id, "award_type": "奖励",
                                     "title": "自助加分", "reason": "试图自助加分",
                                     "happened_on": local_time()[:10],
                                     "extra_points": "100"}, follow_redirects=False)
    check("普通会员不能录奖惩（403）", w.status_code == 403, f"got {w.status_code}")
    full_export = client.get("/members/export")
    check("普通会员不能看全量导出", full_export.status_code in (403, 401),
          f"got {full_export.status_code}")

    client.post("/auth/logout", data={"csrf_token": token(client, "/me")}, follow_redirects=False)
    check("未登录访问统计 401", client.get("/stats/members").status_code == 401)

for name, verdict, detail in results:
    print(f"{verdict}\t{name}\t{detail}")
failed = [r for r in results if r[1] == "FAIL"]
print(f"\n合计 {len(results)} 项，失败 {len(failed)} 项")
sys.exit(1 if failed else 0)
