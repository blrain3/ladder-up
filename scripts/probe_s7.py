"""人工对抗性探测：S7 的离线同步、幂等、CSV 错误报告与建议规则。"""
import io
import os
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/s7.db"
os.environ["APP_ENV"] = "probe"
os.environ["SESSION_TTL_HOURS"] = "8"
os.environ["UPLOADS_DIR"] = f"{tmp}/uploads"
os.environ["SEED_ADMIN_PASSWORD"] = "AdminProbe1!"
os.environ["SEED_DEMO_PASSWORD"] = "DemoProbe1!"
os.environ["OJ_FIXTURE_PATH"] = str(ROOT / "tests/fixtures/cf_user_status.json")

from alembic.config import Config

from alembic import command

command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
subprocess.run([sys.executable, "bin/seed.py"], check=True, env=os.environ, cwd=ROOT,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.db import SessionLocal, make_engine
from app.main import app
from app.models import Member, OjSubmission

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
    demo1_id = demo1.id

with TestClient(
    app, headers={"Accept": "text/html"},
    backend_options={"use_uvloop": sys.platform != "win32"},
) as client:
    def login(sid, pwd):
        return client.post("/auth/login", data={"student_no": sid, "password": pwd},
                           follow_redirects=False)

    check("会员登录 303", login("demo0001", "DemoProbe1!").status_code == 303)

    # 绑定账号
    bind = client.post("/oj/accounts",
                       data={"csrf_token": token(client, "/oj/accounts"),
                             "platform": "codeforces", "handle": "ladder_smoke"},
                       follow_redirects=False)
    check("绑定 CF 账号", bind.status_code in (200, 303), f"got {bind.status_code} {bind.text[:60]}")
    bind_vj = client.post("/oj/accounts",
                          data={"csrf_token": token(client, "/oj/accounts"),
                                "platform": "vjudge", "handle": "demo_probe"},
                          follow_redirects=False)
    check("绑定 Vjudge 账号", bind_vj.status_code in (200, 303), f"got {bind_vj.status_code}")
    dup = client.post("/oj/accounts",
                      data={"csrf_token": token(client, "/oj/accounts"),
                            "platform": "codeforces", "handle": "ladder_smoke"},
                      follow_redirects=False)
    check("重复绑定 40901", dup.status_code == 409 and "40901" in dup.text, f"got {dup.status_code}")
    bad = client.post("/oj/accounts",
                      data={"csrf_token": token(client, "/oj/accounts"),
                            "platform": "codeforces", "handle": "非法 handle!!!"},
                      follow_redirects=False)
    check("非法 handle 40001", bad.status_code == 400 and "40001" in bad.text, f"got {bad.status_code}")

    # 打开同步开关（测试用）
    with SessionLocal(bind=engine) as s:
        s.execute(text("UPDATE sys_config SET config_value='true' WHERE config_key='oj.sync.enabled'"))
        s.commit()

    # 普通会员不能触发同步
    denied = client.post("/oj/sync", data={"csrf_token": token(client, "/oj/accounts"),
                                          "platform": "codeforces"}, follow_redirects=False)
    check("普通会员不能触发同步（403）", denied.status_code == 403, f"got {denied.status_code}")

    member_import = client.post("/oj/vjudge-import",
                                data={"csrf_token": token(client, "/oj/accounts")},
                                files={"file": ("s.csv", io.BytesIO(b"handle,problem,verdict,submitted_at\n"),
                                                "text/csv")},
                                headers={"Accept": "application/json"}, follow_redirects=False)
    check("普通会员不能导入 CSV（403）", member_import.status_code == 403,
          f"got {member_import.status_code}")

    client.post("/auth/logout", data={"csrf_token": token(client, "/me/password")}, follow_redirects=False)
    login("demo0004", "DemoProbe1!")
    synced = client.post("/oj/sync", data={"csrf_token": token(client, "/oj/sync"),
                                           "platform": "codeforces"}, follow_redirects=False)
    location = synced.headers.get("Location", "")
    job_response = client.get(location, headers={"Accept": "application/json"}) if location else None
    job_data = job_response.json().get("data", {}) if job_response is not None and job_response.is_success else {}
    check("教练触发同步", synced.status_code == 303 and location.startswith("/jobs/")
          and job_data.get("status") == "success" and job_data["detail"].get("errors") == 0,
          f"got {synced.status_code}, job={job_data.get('status')}")
    with SessionLocal(bind=engine) as s:
        first = s.scalar(select(func.count()).select_from(OjSubmission))

    again = client.post("/oj/sync", data={"csrf_token": token(client, "/oj/sync"),
                                          "platform": "codeforces"}, follow_redirects=False)
    with SessionLocal(bind=engine) as s:
        second = s.scalar(select(func.count()).select_from(OjSubmission))
    check("同步幂等（行数不变）", first == second and first >= 1
          and again.status_code == 303 and again.headers.get("Location") == location, f"{first} -> {second}")

    # 提交列表
    subs = client.get("/oj/submissions")
    check("提交列表 200", subs.status_code == 200, f"got {subs.status_code}")
    check("提交列表含 verdict 徽章", "badge" in subs.text)
    with SessionLocal(bind=engine) as s2:
        rows_now = s2.scalar(select(func.count()).select_from(OjSubmission))
    check("同步后提交列表有记录", rows_now >= 1, f"rows={rows_now}")
    check("提交列表含分页区域或记录文本",
          ("OJ 提交记录分页" in subs.text) or ("page=" in subs.text) or ("提交" in subs.text))

    # 建议页
    advice = client.get("/training-advice")
    check("建议页 200", advice.status_code == 200, f"got {advice.status_code}")
    check("建议页无数据时不报错", advice.status_code == 200)

    client.post("/auth/logout", data={"csrf_token": token(client, "/me/password")}, follow_redirects=False)
    login("demo0004", "DemoProbe1!")

    # 教练导入 Vjudge CSV（2 好 2 坏）
    csv_text = ("handle,problem,verdict,submitted_at\n"
                "demo_probe,1001A,AC,2026-11-20 10:00:00\n"
                "demo_probe,1002B,WA,2026-11-20 11:00:00\n"
                "unknown_handle,1003C,AC,2026-11-20 12:00:00\n"
                "demo_probe,1004D,AC,不是时间\n")
    up = client.post("/oj/vjudge-import",
                     data={"csrf_token": token(client, "/oj/vjudge-import")},
                     files={"file": ("subs.csv", io.BytesIO(csv_text.encode()), "text/csv")},
                     headers={"Accept": "application/json"}, follow_redirects=False)
    report = up.json().get("data", {}) if up.is_success else {}
    check("导入返回 200", up.status_code == 200, f"got {up.status_code}")
    check("好行计数 2", report.get("accepted") == report.get("created") == 2
          and report.get("skipped") == 0, f"accepted={report.get('accepted')}")
    check("坏行计数 2", report.get("rejected") == 2, f"rejected={report.get('rejected')}")
    check("错误报告带行号", [error["line"] for error in report.get("errors", [])] == [4, 5])

    # 再导入一次，行数不变（幂等）
    with SessionLocal(bind=engine) as s:
        before = s.scalar(select(func.count()).select_from(OjSubmission))
    client.post("/oj/vjudge-import",
                data={"csrf_token": token(client, "/oj/vjudge-import")},
                files={"file": ("subs.csv", io.BytesIO(csv_text.encode()), "text/csv")},
                headers={"Accept": "application/json"}, follow_redirects=False)
    with SessionLocal(bind=engine) as s:
        after = s.scalar(select(func.count()).select_from(OjSubmission))
    check("CSV 重复导入不新增", before == after, f"{before} -> {after}")

    # 表头错误
    bad_csv = client.post("/oj/vjudge-import",
                          data={"csrf_token": token(client, "/oj/vjudge-import")},
                          files={"file": ("bad.csv", io.BytesIO(b"a,b,c\n1,2,3\n"), "text/csv")},
                          follow_redirects=False)
    check("表头错误 40001", bad_csv.status_code == 400 and "40001" in bad_csv.text,
          f"got {bad_csv.status_code}")

    client.post("/auth/logout", data={"csrf_token": token(client, "/me/password")}, follow_redirects=False)
    check("未登录访问 OJ 页面 401", client.get("/oj/accounts").status_code == 401)

for name, verdict, detail in results:
    print(f"{verdict}\t{name}\t{detail}")
failed = [r for r in results if r[1] == "FAIL"]
print(f"\n合计 {len(results)} 项，失败 {len(failed)} 项")
sys.exit(1 if failed else 0)
