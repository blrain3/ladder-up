"""人工对抗性探测：S5 的五个口径（录分覆盖、考核幂等、转正条件、退会/除名断会话、事由必填）。"""
import os
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/s5.db"
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
from sqlalchemy import func, select

from app.db import SessionLocal, make_engine
from app.main import app
from app.models import AuditLog, JobRun, Member, MemberAssessment, TestScore

TOKEN_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
results: list[tuple[str, str, str]] = []


def check(name, ok, detail=""):
    results.append((name, "PASS" if ok else "FAIL", detail))


def token(client, url="/me/password"):
    """页面里取 CSRF；/me 页面没有表单，退化到改密页。"""
    for candidate in (url, "/me/password"):
        m = TOKEN_RE.search(client.get(candidate).text)
        if m:
            return m.group(1)
    return ""


engine = make_engine(os.environ["DATABASE_URL"])

with TestClient(app, headers={"Accept": "text/html"},
                backend_options={"use_uvloop": sys.platform != "win32"}) as client:
    def login(sid, pwd):
        return client.post("/auth/login", data={"student_no": sid, "password": pwd},
                           follow_redirects=False)

    def logout():
        return client.post("/auth/logout", data={"csrf_token": token(client, "/me")},
                           follow_redirects=False)

    check("教练登录 303", login("demo0004", "DemoProbe1!").status_code == 303)

    # 建周测
    r = client.post("/assessment-tests",
                    data={"csrf_token": token(client, "/assessment-tests"),
                          "title": "第 1 次周测", "test_type": "周测", "held_on": "2026-11-20"},
                    follow_redirects=False)
    check("建周测", r.status_code in (200, 303), f"got {r.status_code} {r.text[:60]}")

    with SessionLocal(bind=engine) as s:
        from app.models import AssessmentTest
        test_row = s.scalar(select(AssessmentTest))
        test_id = test_row.id if test_row else 1
        demo1 = s.scalar(select(Member).where(Member.student_no == "demo0001"))
        demo2 = s.scalar(select(Member).where(Member.student_no == "demo0002"))
        assert demo1 is not None and demo2 is not None
        demo1_id, demo2_id = demo1.id, demo2.id

    # 录分：首次
    first = client.post(f"/assessment-tests/{test_id}/scores",
                        data={"csrf_token": token(client, f"/assessment-tests/{test_id}/scores"),
                              "member_ids": [demo1_id, demo2_id],
                              f"version_{demo1_id}": "0", f"version_{demo2_id}": "0",
                              f"solved_{demo1_id}": "4", f"score_{demo1_id}": "400",
                              f"solved_{demo2_id}": "1", f"score_{demo2_id}": "100"},
                        follow_redirects=False)
    check("首次录分", first.status_code in (200, 303), f"got {first.status_code} {first.text[:60]}")
    with SessionLocal(bind=engine) as s:
        rows_before = s.scalar(select(func.count()).select_from(TestScore))

    # 覆盖式：再次录分不新增行
    client.post(f"/assessment-tests/{test_id}/scores",
                         data={"csrf_token": token(client, f"/assessment-tests/{test_id}/scores"),
                               "member_ids": [demo1_id],
                               f"version_{demo1_id}": "1", f"solved_{demo1_id}": "5", f"score_{demo1_id}": "500"},
                         follow_redirects=False)
    with SessionLocal(bind=engine) as s:
        rows_after = s.scalar(select(func.count()).select_from(TestScore))
        score = s.scalar(select(TestScore).where(TestScore.member_id == demo1_id))
        assert score is not None
        audited = s.scalar(select(func.count()).select_from(AuditLog)
                           .where(AuditLog.action.like("score%update%")))
    check("重复录分覆盖而非新增", rows_before == rows_after, f"{rows_before} -> {rows_after}")
    check("覆盖后分数已更新", score.solved_count == 5, f"solved={score.solved_count}")
    check("覆盖写审计", (audited or 0) >= 1, f"audit={audited}")

    # 考核幂等（走 CLI，模拟两遍）
    def run_assess():
        return subprocess.run([sys.executable, "bin/job.py", "assess", "--term", "2026-2027-1"],
                              cwd=ROOT, env=os.environ, capture_output=True, text=True)

    r1 = run_assess()
    with SessionLocal(bind=engine) as s:
        rows_first = s.scalar(select(func.count()).select_from(MemberAssessment)
                              .where(MemberAssessment.term == "2026-2027-1"))
    r2 = run_assess()
    with SessionLocal(bind=engine) as s:
        assess_rows = s.scalar(select(func.count()).select_from(MemberAssessment)
                               .where(MemberAssessment.term == "2026-2027-1"))
        job_rows = s.scalars(select(JobRun).where(JobRun.job_name.like("assess%"))).all()
    check("assess 命令成功两次", r1.returncode == 0 and r2.returncode == 0,
          f"{r1.returncode}/{r2.returncode} {r1.stderr[-120:]}")
    check("考核不因重跑新增（行数不变）", rows_first == assess_rows and (rows_first or 0) >= 1,
          f"{rows_first} -> {assess_rows}")
    check("job_run 只一行", len(job_rows) == 1, f"rows={len(job_rows)}")
    check("job_run attempts 自增", getattr(job_rows[0], "attempts", 0) >= 2,
          f"attempts={getattr(job_rows[0], 'attempts', None)}")

    # 转正：先看失败原因（完成率不足）
    logout()
    login("demo0001", "DemoProbe1!")
    check("普通会员访问待转正页 403",
          client.get("/promotions").status_code == 403)
    logout()
    login("demo0004", "DemoProbe1!")
    promos = client.get("/promotions").text
    check("待转正页可访问", "可转正" in promos or "未达标" in promos or "暂无数据" in promos)

    promote_missing = client.post(f"/members/{demo1_id}/promote",
                                 data={"csrf_token": token(client, "/promotions")},
                                 follow_redirects=False)
    check("条件不足转正被拒（40002）",
          promote_missing.status_code == 400 and "40002" in promote_missing.text,
          f"got {promote_missing.status_code} {promote_missing.text[:80]}")

    # 退会：本人操作 + 断会话
    logout()
    login("demo0002", "DemoProbe1!")
    old = client.cookies.get("sid")
    status_before = client.get("/me").json()["data"]["member_status"]
    wd = client.post(f"/members/{demo2_id}/leave",
                     data={"csrf_token": token(client, "/me"), "reason": "个人原因"},
                     follow_redirects=False)
    check("本人退会申请保持状态和会话", wd.status_code == 200
          and client.get("/me").json()["data"]["member_status"] == status_before)
    login("demo0004", "DemoProbe1!")
    approved = client.post(f"/members/{demo2_id}/leave/approve",
                           data={"csrf_token": token(client)}, follow_redirects=False)
    with SessionLocal(bind=engine) as s:
        updated_member = s.get(Member, demo2_id)
        assert updated_member is not None
        status_after = updated_member.member_status
    check("退会审批后状态变更", approved.status_code == 200 and status_after == "已退会",
          f"status={status_after}")
    reviewer_sid = client.cookies["sid"]
    client.cookies.set("sid", old, domain="testserver.local", path="/")
    again = client.get("/me", headers={"Accept": "application/json"})
    check("退会后旧会话失效", again.status_code == 401 and again.json()["code"] == 40101,
          f"got {again.status_code}")
    client.cookies.set("sid", reviewer_sid, domain="testserver.local", path="/")

    # 除名：事由必填 + 断会话
    login("demo0004", "DemoProbe1!")
    no_reason = client.post(f"/members/{demo1_id}/expel",
                            data={"csrf_token": token(client, "/promotions")},
                            follow_redirects=False)
    check("除名缺事由 40001", no_reason.status_code == 400 and "40001" in no_reason.text,
          f"got {no_reason.status_code}")
    ok_expel = client.post(f"/members/{demo1_id}/expel",
                           data={"csrf_token": token(client, "/me"),
                                 "reason": "连续两次考核未通过并缺席集训"},
                           follow_redirects=False)
    with SessionLocal(bind=engine) as s:
        m1 = s.get(Member, demo1_id)
        assert m1 is not None
        audited = s.scalar(select(func.count()).select_from(AuditLog)
                           .where(AuditLog.action.like("%expel%")))
    check("除名成功", ok_expel.status_code in (200, 303), f"got {ok_expel.status_code}")
    check("除名后状态为已除名", m1.member_status == "已除名", f"status={m1.member_status}")
    check("除名写审计", (audited or 0) >= 1, f"audit={audited}")
    logout()
    relogin = login("demo0001", "DemoProbe1!")
    check("除名后无法再登录", relogin.status_code in (401, 403), f"got {relogin.status_code}")

for name, verdict, detail in results:
    print(f"{verdict}\t{name}\t{detail}")
failed = [r for r in results if r[1] == "FAIL"]
print(f"\n合计 {len(results)} 项，失败 {len(failed)} 项")
sys.exit(1 if failed else 0)
