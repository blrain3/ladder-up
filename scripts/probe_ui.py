"""人工探测：前端样式迁移（BS5 + 图标 + 确认框 + 本地资源）。"""
import os
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/ui.db"
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

from app.main import app

TOKEN_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
results = []


def check(name, ok, detail=""):
    results.append((name, "PASS" if ok else "FAIL", detail))


def token(client, url="/me/password"):
    m = TOKEN_RE.search(client.get(url).text)
    return m.group(1) if m else ""


with TestClient(
    app, headers={"Accept": "text/html"}, backend_options={"use_uvloop": sys.platform != "win32"},
) as client:
    anon = client.get("/login").text
    check("登录页无 CDN 引用",
          "cdn." not in anon and "https://" not in anon.replace("http://www.w3.org", ""),
          "含外部域名" if "cdn." in anon else "")
    check("登录页引用本地 BS5 CSS", "/static/bootstrap.min.css" in anon)
    check("登录页引用本地图标字体", "/static/bootstrap-icons.css" in anon)
    check("登录页含 Bootstrap 5 结构", "navbar" in anon or "container" in anon)

    client.post("/auth/login", data={"student_no": "demo0001", "password": "DemoProbe1!"},
                follow_redirects=False)
    members = client.get("/members").text
    check("会员列表含 table-responsive", "table-responsive" in members)
    check("会员列表含状态徽章", "badge" in members)
    check("会员列表含图标类", "bi-" in members)
    check("会员列表含 navbar-toggler（手机可折叠）", "navbar-toggler" in members)
    check("会员列表含当前会员姓名", "示例会员" in members)
    check("会员列表含「奖惩」入口（普通会员）", 'href="/awards?scope=mine"' in members)
    check("会员列表不含「统计」入口（普通会员）", "/stats/members" not in members)

    client.post("/auth/logout", data={"csrf_token": token(client, "/me/password")},
                follow_redirects=False)
    client.post("/auth/login", data={"student_no": "demo0004", "password": "DemoProbe1!"},
                follow_redirects=False)
    coach = client.get("/members").text
    check("教练导航含题库/训练/奖惩入口",
          "/problems" in coach and "/training-tasks" in coach and "/awards" in coach)

    client.post("/knowledge-points", data={"csrf_token": token(client, "/knowledge-points"),
                                         "name": "界面探测节点"}, follow_redirects=False)
    kps = client.get("/knowledge-points").text
    check("知识点页删除表单带 data-confirm", "data-confirm" in kps, "未找到确认属性")
    check("知识点页用 BS5 卡片或表格类", ("card" in kps or "table" in kps))

    awards = client.get("/awards").text
    check("奖惩页 200 且含图标", "bi-" in awards)

    client.post("/auth/logout", data={"csrf_token": token(client, "/me/password")},
                follow_redirects=False)
    client.post("/auth/login", data={"student_no": "admin0001", "password": "AdminProbe1!"},
                follow_redirects=False)
    admin = client.get("/members").text
    check("管理员导航含统计入口", "/stats/members" in admin)

    # 打印样式：会员证
    card = client.get("/members/2/card").text
    check("会员证加载含 @media print 的本地样式",
          'href="/static/app.css"' in card and "@media print" in client.get("/static/app.css").text)
    check("会员证含 no-print 标记", "no-print" in card)

    # 表单字段未破坏
    check("会员列表仍有登出表单 post /auth/logout", 'action="/auth/logout"' in admin)
    check("导航内仍有 csrf_token 隐藏字段", 'name="csrf_token"' in admin)

for name, verdict, detail in results:
    print(f"{verdict}\t{name}\t{detail}")
failed = [r for r in results if r[1] == "FAIL"]
print(f"\n合计 {len(results)} 项，失败 {len(failed)} 项")
sys.exit(1 if failed else 0)
