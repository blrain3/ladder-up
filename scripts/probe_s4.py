"""人工对抗性探测：S4 训练的四个口径（下发冻结、打卡唯一、补卡不新增行、进度派生）。"""
import os
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/s4.db"
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
from app.models import Checkin, Member, Problem, TaskAssignee
from app.models.base import local_time
from app.security import hash_password

TOKEN_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
results = []


def check(name, ok, detail=""):
    results.append((name, "PASS" if ok else "FAIL", detail))


def token(client, url):
    m = TOKEN_RE.search(client.get(url).text)
    return m.group(1) if m else ""


with TestClient(app, headers={"Accept": "text/html"},
                backend_options={"use_uvloop": sys.platform != "win32"}) as client:
    def login(sid, pwd):
        return client.post("/auth/login", data={"student_no": sid, "password": pwd},
                           follow_redirects=False)

    check("教练登录 303", login("demo0004", "DemoProbe1!").status_code == 303)

    # 建计划
    client.post("/training-plans", data={"csrf_token": token(client, "/training-plans"),
                                        "term": "2026-2027-1", "goal": "冬训", "intensity": "高"},
                follow_redirects=False)
    # 建阶段
    engine = make_engine(os.environ["DATABASE_URL"])
    with SessionLocal(bind=engine) as s0:
        from app.models import TrainingPlan as _TP
        plan_row = s0.scalar(select(_TP))
        plan_id_val = plan_row.id if plan_row else 1
    client.post("/training-phases", data={"csrf_token": token(client, "/training-phases"),
                                         "plan_id": plan_id_val, "name": "基础",
                                         "start_on": "2026-11-01", "end_on": "2027-01-31"},
                follow_redirects=False)

    with SessionLocal(bind=engine) as s:
        from app.models import TrainingPhase
        phase = s.scalar(select(TrainingPhase))
        problems = [Problem(title=f"P{i}", difficulty="提高", problem_type="程序",
                            source="自建", created_at=local_time(), updated_at=local_time())
                    for i in range(1, 3)]
        s.add_all(problems)
        s.commit()
        pid_a, pid_b = problems[0].id, problems[1].id
        phase_id_val = phase.id if phase else 1

    # 建任务（草稿）+ 下发
    resp = client.post("/training-tasks",
                       data={"csrf_token": token(client, "/training-tasks"),
                             "phase_id": phase_id_val, "title": "第一周作业",
                             "assignee_type": "指定会员",
                             "member_ids": "2",
                             "problem_ids": [pid_a, pid_b]},
                       follow_redirects=False)
    check("建任务（草稿）", resp.status_code in (200, 201, 303), f"got {resp.status_code} {resp.text[:60]}")

    with SessionLocal(bind=engine) as s:
        from app.models import TrainingTask
        task = s.scalar(select(TrainingTask))
        task_id = task.id
        check("草稿状态未下发", task.published_at is None, f"published_at={task.published_at}")

    asg = client.post(f"/training-tasks/{task_id}/assign",
                      data={"csrf_token": token(client, "/training-tasks"),
                            "assignee_type": "指定会员", "member_ids": "2"},
                      follow_redirects=False)
    check("设置指派并下发", asg.status_code == 303, f"got {asg.status_code}")
    with SessionLocal(bind=engine) as s:
        task = s.get(TrainingTask, task_id)
        check("下发状态已保存", task.status == "已下发" and task.published_at is not None)
    again = client.post(f"/training-tasks/{task_id}/assign",
                        data={"csrf_token": token(client, "/training-tasks"),
                              "assignee_type": "指定会员", "member_ids": "2"}, follow_redirects=False)
    check("重复下发被拒（40002）", again.status_code == 400 and "40002" in again.text,
          f"got {again.status_code}")

    with SessionLocal(bind=engine) as s:
        rows = s.scalars(select(TaskAssignee).where(TaskAssignee.task_id == task_id)).all()
        check("下发写入指派名单", [(row.assignee_type, row.assignee_id) for row in rows] == [("会员", 2)], f"rows={len(rows)}")
        # 模拟「下发后新增会员」
        newbie = Member(student_no="202499998888", name="后来者", role="member",
                        member_type="观察会员", member_status="观察会员",
                        password_hash=hash_password("DemoProbe1!"), created_at=local_time(), updated_at=local_time())
        s.add(newbie)
        s.commit()

    client.post("/auth/logout", data={"csrf_token": token(client, "/me")}, follow_redirects=False)

    # 后来者登录，看不到任务
    newcomer_login = login("202499998888", "DemoProbe1!")
    check("后来者详情返回 404",
          newcomer_login.status_code == 303 and client.get(f"/training-tasks/{task_id}").status_code == 404)

    # demo0001 打卡（1 解 1 未解）
    login("demo0001", "DemoProbe1!")
    detail = client.get(f"/training-tasks/{task_id}")
    check("被指派会员可看任务详情", detail.status_code == 200, f"got {detail.status_code}")
    ck = client.post("/checkins", data={"csrf_token": token(client, f"/training-tasks/{task_id}"),
                                       "task_id": task_id,
                                       "checkin_date": local_time()[:10],
                                       "minutes": 60, "solved_count": 1,
                                       "solved_problem_ids": [pid_a]}, follow_redirects=False)
    check("打卡成功（1/2）", ck.status_code in (200, 303), f"got {ck.status_code} {ck.text[:60]}")

    dup = client.post("/checkins", data={"csrf_token": token(client, f"/training-tasks/{task_id}"),
                                        "task_id": task_id, "checkin_date": local_time()[:10],
                                        "minutes": 30, "solved_count": 0}, follow_redirects=False)
    check("同日重复打卡 40901", dup.status_code == 409 and "40901" in dup.text, f"got {dup.status_code}")

    mismatch = client.post("/checkins", data={"csrf_token": token(client, f"/training-tasks/{task_id}"),
                                             "task_id": task_id, "checkin_date": local_time()[:10],
                                             "minutes": 30, "solved_count": 2,
                                             "solved_problem_ids": [pid_a]}, follow_redirects=False)
    check("solved_count 与明细不一致 40001",
          mismatch.status_code == 400 and "40001" in mismatch.text, f"got {mismatch.status_code}")

    prog = client.get(f"/training-progress?task_id={task_id}")
    check("进度页显示 1/2", prog.status_code == 200 and ("1 / 2" in prog.text or "1/2" in prog.text),
          f"got {prog.status_code}")

    # 补卡：已有记录 -> 不新增行
    client.post("/auth/logout", data={"csrf_token": token(client, "/me")}, follow_redirects=False)
    login("demo0004", "DemoProbe1!")
    with SessionLocal(bind=engine) as s:
        before = s.scalar(select(func.count()).select_from(Checkin))
    mk = client.post("/checkins/makeup",
                     data={"csrf_token": token(client, "/training-tasks"),
                           "member_id": 2, "task_id": task_id,
                           "checkin_date": local_time()[:10], "note": "补卡"}, follow_redirects=False)
    with SessionLocal(bind=engine) as s:
        after = s.scalar(select(func.count()).select_from(Checkin))
    check("补卡成功", mk.status_code in (200, 303), f"got {mk.status_code}")
    check("补卡未新增行", before == after, f"{before} -> {after}")

    # 第 4 口径：软删题目不计入分母
    with SessionLocal(bind=engine) as s:
        p_del = s.get(Problem, pid_b)
        p_del.deleted_at = local_time()
        s.commit()
    from app.training import task_progress
    with SessionLocal(bind=engine) as s:
        prog_data = task_progress(s, task_id, 2)
    check("软删题目从分母剔除", prog_data["total"] == 1, f"total={prog_data['total']}")

for name, verdict, detail in results:
    print(f"{verdict}\t{name}\t{detail}")
failed = [r for r in results if r[1] == "FAIL"]
print(f"\n合计 {len(results)} 项，失败 {len(failed)} 项")
sys.exit(1 if failed else 0)
