"""演练用数据构造：通过 HTTP 接口造一批业务数据（不直接写库）。"""
import io
import os
import pathlib
import re
import sys

ROOT = pathlib.Path("/root/projects/ladder-up")
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.main import app

TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
       b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00"
       b"\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")

done = {"kp": 0, "problem": 0, "task": 0, "checkin": 0, "photo": 0, "award": 0, "test": 0, "score": 0}


def tok(c, url):
    m = TOKEN.search(c.get(url).text)
    return m.group(1) if m else ""


with TestClient(app, headers={"Accept": "text/html"}, base_url="http://drill") as c:
    c.post("/auth/login", data={"student_no": "admin0001",
                                "password": os.environ["SEED_ADMIN_PASSWORD"]},
           follow_redirects=False)

    # 知识点：两级
    for name, parent in (("图论", ""), ("最短路", "1")):
        data = {"csrf_token": tok(c, "/knowledge-points"), "name": name}
        if parent:
            data["parent_id"] = parent
        r = c.post("/knowledge-points", data=data, follow_redirects=False)
        done["kp"] += r.status_code in (200, 303)

    # 题目 6 道
    for i in range(1, 7):
        r = c.post("/problems", data={
            "csrf_token": tok(c, "/problems"), "title": f"演练题 {i}",
            "difficulty": ["入门", "提高", "进阶"][i % 3], "problem_type": "程序",
            "source": "自建", "kp_ids": "2", "statement": f"第 {i} 题的题面",
            "sample_in": "1 2", "sample_out": "3"}, follow_redirects=False)
        done["problem"] += r.status_code in (200, 303)

    # 计划 → 阶段 → 任务 → 下发
    c.post("/training-plans", data={"csrf_token": tok(c, "/training-plans"),
                                    "term": "2026-2027-1", "goal": "演练", "intensity": "中"},
           follow_redirects=False)
    c.post("/training-phases", data={"csrf_token": tok(c, "/training-phases"),
                                     "plan_id": "1", "name": "基础",
                                     "start_on": "2026-11-01", "end_on": "2027-01-15"},
           follow_redirects=False)
    r = c.post("/training-tasks", data={"csrf_token": tok(c, "/training-tasks"),
                                        "phase_id": "1", "title": "演练任务",
                                        "deadline_at": "2027-01-10 23:59",
                                        "assignee_type": "指定会员",
                                        "problem_ids": ["1", "2", "3"]}, follow_redirects=False)
    done["task"] += r.status_code in (200, 303)
    c.post("/training-tasks/1/assign", data={"csrf_token": tok(c, "/training-tasks"),
                                             "assignee_type": "指定会员", "member_ids": "2"},
           follow_redirects=False)
    c.post("/training-tasks/1/publish", data={"csrf_token": tok(c, "/training-tasks")},
           follow_redirects=False)

    # 会员照片 2 张（走 attachment + 文件）
    for mid in ("2", "3"):
        r = c.post(f"/members/{mid}/photo", data={"csrf_token": tok(c, "/members")},
                   files={"file": (f"m{mid}.png", io.BytesIO(PNG), "image/png")},
                   follow_redirects=False)
        done["photo"] += r.status_code in (200, 303)

    # 奖惩 2 条
    for title, points, kind in (("省赛铜牌", "150", "奖励"), ("缺勤", "-50", "惩处")):
        r = c.post("/awards", data={"csrf_token": tok(c, "/awards"), "member_id": "2",
                                    "award_type": kind, "title": title, "reason": "演练数据",
                                    "happened_on": "2026-11-20", "extra_points": points},
                   follow_redirects=False)
        done["award"] += r.status_code in (200, 303)

    # 周测 + 录分
    c.post("/assessment-tests", data={"csrf_token": tok(c, "/assessment-tests"),
                                      "title": "演练周测", "test_type": "周测",
                                      "held_on": "2026-11-21"}, follow_redirects=False)
    r = c.post("/assessment-tests/1/scores", data={
        "csrf_token": tok(c, "/assessment-tests/1/scores"),
        "member_ids": ["2", "3"], "solved_2": "3", "score_2": "300",
        "solved_3": "1", "score_3": "100"}, follow_redirects=False)
    done["score"] += r.status_code in (200, 303)

    # 会员打卡（demo0001）
    c.post("/auth/logout", data={"csrf_token": tok(c, "/me/password")}, follow_redirects=False)
    c.post("/auth/login", data={"student_no": "demo0001",
                                "password": os.environ["SEED_DEMO_PASSWORD"]},
           follow_redirects=False)
    r = c.post("/checkins", data={"csrf_token": tok(c, "/training-tasks/1"),
                                  "task_id": "1", "checkin_date": "2026-11-22",
                                  "minutes": "90", "solved_count": "2",
                                  "solved_problem_ids": ["1", "2"]}, follow_redirects=False)
    done["checkin"] += r.status_code in (200, 303)

print("构造结果:", done)
