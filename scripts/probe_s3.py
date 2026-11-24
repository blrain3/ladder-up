"""人工对抗性探测：S3 题库的树不变量、检索、XSS 与权限。

不修改实现，只产出验收证据。单客户端顺序切换身份。
"""
import os
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/s3.db"
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


with TestClient(
    app, headers={"Accept": "text/html"}, backend_options={"use_uvloop": sys.platform != "win32"},
) as client:
    def token(url="/knowledge-points"):
        if url == "/me":
            return client.get(url, headers={"Accept": "application/json"}).json()["data"]["csrf_token"]
        found = TOKEN_RE.search(client.get(url).text)
        return found.group(1) if found else ""

    def login(student_no, password):
        return client.post("/auth/login", data={"student_no": student_no, "password": password},
                           follow_redirects=False)

    def kp_id_from(page):
        ids = re.findall(r'/knowledge-points/(\d+)', page)
        return ids

    check("教练登录 303", login("demo0004", "DemoProbe1!").status_code == 303)

    # 建父节点
    r = client.post("/knowledge-points", data={"csrf_token": token(), "name": "图论"},
                    follow_redirects=False)
    check("建父知识点", r.status_code in (200, 201, 303), f"got {r.status_code}")
    ids = kp_id_from(client.get("/knowledge-points").text)
    parent = ids[0] if ids else "1"

    # 同级重名
    dup = client.post("/knowledge-points", data={"csrf_token": token(), "name": "图论"},
                      follow_redirects=False)
    check("同级重名被拒（409/40901）",
          dup.status_code == 409 or "40901" in dup.text, f"got {dup.status_code}")

    # 建子节点
    r = client.post("/knowledge-points", data={"csrf_token": token(), "name": "最短路",
                                              "parent_id": parent}, follow_redirects=False)
    check("建子知识点", r.status_code in (200, 201, 303), f"got {r.status_code}")

    # 成环：把父节点移到自己的子节点下
    page = client.get("/knowledge-points").text
    ids = kp_id_from(page)
    child = next((i for i in ids if i != parent), None)
    if child:
        cyc = client.post(f"/knowledge-points/{parent}/move",
                          data={"csrf_token": token(), "parent_id": child}, follow_redirects=False)
        check("把父节点移到自身后代被拒（40002）",
              cyc.status_code in (400, 409) and ("40002" in cyc.text or "40001" in cyc.text),
              f"got {cyc.status_code} {cyc.text[:60]}")
    else:
        check("把父节点移到自身后代被拒（40002）", False, "未找到子节点 id")

    # 删除有子节点的父节点
    dele = client.post(f"/knowledge-points/{parent}/delete", data={"csrf_token": token()},
                       follow_redirects=False)
    check("删除含子节点的知识点被拒（40002）",
          dele.status_code == 400 and "40002" in dele.text, f"got {dele.status_code}")

    # 录题（题面含 script）
    xss = '<script>alert(1)</script> 求最短路 <img src=x onerror=alert(2)>'
    form = {"csrf_token": token("/problems"), "title": "单源最短路", "difficulty": "提高",
            "problem_type": "程序", "source": "自建", "kp_ids": child or parent,
            "statement": xss, "sample_in": "3 3", "sample_out": "0"}
    created = client.post("/problems", data=form, follow_redirects=False)
    check("录题成功", created.status_code in (200, 201, 303), f"got {created.status_code} {created.text[:60]}")

    listing = client.get("/problems").text
    detail_ids = re.findall(r'/problems/(\d+)"', listing)
    pid = detail_ids[0] if detail_ids else "1"

    detail = client.get(f"/problems/{pid}").text
    check("题面 script 被转义", "<script" not in detail and "&lt;script" in detail)
    check("题面 onerror 未生效", "<img src=x onerror=" not in detail and "&lt;img src=x onerror=" in detail)

    # 子树检索：按父节点检索应命中挂在子节点的题
    subtree = client.get(f"/problems?kp={parent}").text
    check("按父知识点检索命中子知识点下的题", "单源最短路" in subtree)
    leaf = client.get(f"/problems?kp={child or parent}").text
    check("按子知识点检索命中", "单源最短路" in leaf)

    # 空结果
    empty = client.get("/problems?keyword=不存在的题目xyz")
    check("空结果不报错且 total=0", empty.status_code == 200 and ("没有匹配" in empty.text or "total=0" in empty.text),
          f"got {empty.status_code}")

    # 题解 XSS
    sol = client.post(f"/problems/{pid}/solutions",
                      data={"csrf_token": token(f"/problems/{pid}"),
                            "content_md": "[点我](javascript:alert(1))"}, follow_redirects=False)
    check("提交题解", sol.status_code in (200, 201, 303), f"got {sol.status_code}")
    page = client.get(f"/problems/{pid}").text
    check("题解 javascript: 链接被过滤", "javascript:" not in page)

    # 权限：普通会员不可写
    client.post("/auth/logout", data={"csrf_token": token("/me")}, follow_redirects=False)
    check("普通会员登录 303", login("demo0001", "DemoProbe1!").status_code == 303)
    read_ok = client.get("/problems")
    check("普通会员可读题目列表", read_ok.status_code == 200, f"got {read_ok.status_code}")
    write = client.post("/knowledge-points",
                        data={"csrf_token": token("/problems"), "name": "会员不应能建"},
                        follow_redirects=False)
    check("普通会员不能建知识点（403）", write.status_code == 403, f"got {write.status_code}")

    client.post("/auth/logout", data={"csrf_token": token("/me")}, follow_redirects=False)
    anon = client.get("/problems")
    check("未登录访问题目 401", anon.status_code == 401, f"got {anon.status_code}")

for name, verdict, detail in results:
    print(f"{verdict}\t{name}\t{detail}")
failed = [r for r in results if r[1] == "FAIL"]
print(f"\n合计 {len(results)} 项，失败 {len(failed)} 项")
sys.exit(1 if failed else 0)
