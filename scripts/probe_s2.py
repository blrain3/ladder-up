"""人工对抗性探测：S2 上传、文件读取、申请唯一性、部门范围。

单客户端顺序切换身份，避免多 TestClient 触发 lifespan 重置。
"""
import base64
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
os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/probe.db"
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
results: list[tuple[str, str, str]] = []


def check(name, ok, detail=""):
    results.append((name, "PASS" if ok else "FAIL", detail))


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAFElEQVR4nGNknVbBgA0wYRUdtBIA20cBI1mXgH4AAAAASUVORK5CYII="
)

with TestClient(
    app, headers={"Accept": "text/html"}, backend_options={"use_uvloop": sys.platform != "win32"},
) as client:
    def token(url="/members"):
        found = TOKEN_RE.search(client.get(url).text)
        return found.group(1) if found else ""

    def login(student_no, password):
        return client.post("/auth/login", data={"student_no": student_no, "password": password},
                           follow_redirects=False)

    def logout():
        return client.post("/auth/logout", data={"csrf_token": token()},
                           follow_redirects=False)

    check("管理员登录 303", login("admin0001", "AdminProbe1!").status_code == 303)

    html = client.get("/members").text
    ids = re.findall(r'/members/(\d+)"', html)
    target = next(i for i in ids if i not in {"1", "2"})
    check("会员列表可读且含多个会员", len(set(ids)) >= 2, f"ids={sorted(set(ids))}")

    bad = client.post(f"/members/{target}/photo",
                      data={"csrf_token": token()},
                      files={"file": ("evil.png", io.BytesIO(b"not an image"), "image/png")},
                      follow_redirects=False)
    check("伪装成 png 的文本被拒（400）", bad.status_code == 400, f"got {bad.status_code} {bad.text[:60]}")

    big = client.post(f"/members/{target}/photo",
                      data={"csrf_token": token()},
                      files={"file": ("big.png", io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * (6 * 1024 * 1024)),
                                      "image/png")}, follow_redirects=False)
    check("超过 5MB 被拒（400）", big.status_code == 400, f"got {big.status_code}")

    good = client.post(f"/members/{target}/photo",
                       data={"csrf_token": token()},
                       files={"file": ("ok.png", io.BytesIO(PNG), "image/png")},
                       follow_redirects=False)
    check("真实 png 上传成功", good.status_code in (200, 204, 303), f"got {good.status_code} {good.text[:60]}")

    att = re.search(r'/files/(\d+)', client.get(f"/members/{target}").text)
    check("详情页出现照片链接", att is not None)
    att_id = att.group(1) if att else "1"
    check("管理员读该照片 200", client.get(f"/files/{att_id}").status_code == 200)

    logout()
    check("登出后访问会员列表 401", client.get("/members").status_code == 401)
    check("登出后读该照片 401", client.get(f"/files/{att_id}").status_code == 401)

    check("示例会员登录 303", login("demo0001", "DemoProbe1!").status_code == 303)
    card = client.get("/members/1/card")
    check("普通会员读他人的会员证 403", card.status_code == 403, f"got {card.status_code}")
    check("普通会员读他人照片 403", client.get(f"/files/{att_id}").status_code == 403)

    logout()
    anon_page = client.get("/apply").text
    anon_token = TOKEN_RE.search(anon_page)
    form = {"csrf_token": anon_token.group(1) if anon_token else "",
            "student_no": "202499990001", "name": "探测申请人", "grade": "2025",
            "agree_observation": "1"}
    first = client.post("/apply", data=form, follow_redirects=False)
    check("匿名首次申请显示回执码", first.status_code == 200 and 'id="receipt-code"' in first.text,
          f"got {first.status_code}")
    again = TOKEN_RE.search(client.get("/apply").text)
    form["csrf_token"] = again.group(1) if again else ""
    second = client.post("/apply", data=form, follow_redirects=False)
    check("同一学号重复申请返回 40901",
          second.status_code == 409 and "40901" in second.text, f"got {second.status_code}")

for name, verdict, detail in results:
    print(f"{verdict}\t{name}\t{detail}")
failed = [r for r in results if r[1] == "FAIL"]
print(f"\n合计 {len(results)} 项，失败 {len(failed)} 项")
sys.exit(1 if failed else 0)
