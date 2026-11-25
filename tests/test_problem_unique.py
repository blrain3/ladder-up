import pytest


@pytest.fixture
def writer(client, signed_in, coach):
    csrf = signed_in(client, "coach0001")
    root = client.post(
        "/knowledge-points", json={"name": "图论"}, headers={"X-CSRF-Token": csrf}
    ).json()["data"]
    return csrf, root["id"]


def create(client, csrf, kp_id, **overrides):
    payload = {
        "title": "A+B", "difficulty": "入门", "problem_type": "程序",
        "source": "自建", "kp_ids": [kp_id],
        "sample_in": "1 2", "sample_out": "3",
    }
    payload.update(overrides)
    return client.post("/problems", json=payload, headers={"X-CSRF-Token": csrf})


def test_same_platform_external_second_create_conflicts(client, writer):
    csrf, kp_id = writer
    first = create(client, csrf, kp_id, platform="codeforces", external_id="100A")
    assert first.status_code == 200
    second = create(client, csrf, kp_id, platform="codeforces", external_id="100A")
    assert second.status_code == 409
    assert second.json()["code"] == 40901
    assert second.json()["data"]["id"] == first.json()["data"]["id"]


def test_local_problem_with_empty_platform_and_external_duplicate_title_allowed(client, writer):
    csrf, kp_id = writer
    first = create(client, csrf, kp_id, title="本地重名题")
    assert first.status_code == 200
    second = create(client, csrf, kp_id, title="本地重名题")
    assert second.status_code == 200
    assert first.json()["data"]["id"] != second.json()["data"]["id"]


def test_problem_maintenance_and_soft_delete(client, writer):
    csrf, kp_id = writer
    created = create(client, csrf, kp_id, platform="codeforces", external_id="100A")
    assert created.status_code == 200
    problem_id = created.json()["data"]["id"]
    url = f"/problems/{problem_id}"
    headers = {"X-CSRF-Token": csrf}
    edited = client.put(url, json={
        "title": "更新题目", "difficulty": "提高", "problem_type": "程序",
        "platform": "codeforces", "external_id": "100A",
    }, headers=headers)
    assert edited.status_code == 200
    solution = client.post(url + "/solutions", json={"content_md": "**题解**"}, headers=headers)
    assert solution.status_code == 200
    details = client.get(url).json()["data"]
    assert details["title"] == "更新题目"
    assert len(details["kps"]) == 1 and details["sample_out"] == "3"
    assert client.get(url + "/solutions").json()["data"] == details["solutions"]
    assert client.get("/problems").json()["data"]["items"][0]["solution_count"] == 1

    deleted = client.delete(url, headers=headers)
    assert deleted.status_code == 200
    assert client.get(url).status_code == 404
    assert client.get(url + "/solutions").status_code == 404
    assert client.get("/problems").json()["data"]["total"] == 0
    conflict = create(client, csrf, kp_id, platform="codeforces", external_id="100A")
    assert (conflict.status_code, conflict.json()["code"]) == (409, 40901)
    assert client.get("/problems/stats").json()["data"]["kp_counts"][0]["problem_count"] == 0


def test_form_saves_all_selected_knowledge_points_and_samples(client, writer):
    csrf, first = writer
    second = client.post(
        "/knowledge-points", json={"name": "树形结构"}, headers={"X-CSRF-Token": csrf}
    ).json()["data"]["id"]
    form = {
        "title": "表单多选", "difficulty": "入门", "problem_type": "程序",
        "csrf_token": csrf, "statement": "题面", "kp_ids": [str(first), str(second)],
        "sample_in": "1\n2\n",
    }
    incomplete = client.post("/problems", data=form)
    assert (incomplete.status_code, incomplete.json()["code"]) == (400, 40001)
    assert "样例" in incomplete.json()["message"]
    assert client.get("/problems").json()["data"]["total"] == 0

    response = client.post("/problems", data={**form, "sample_out": "3\n"},
                           headers={"Accept": "text/html"})
    assert response.status_code == 303
    saved = client.get(response.headers["Location"]).json()["data"]
    assert {kp["id"] for kp in saved["kps"]} == {first, second}
    assert len(saved["kps"]) == 2
    assert (saved["sample_in"], saved["sample_out"]) == ("1\n2\n", "3\n")


def test_stats_count_task_uses_and_show_missing_correct_rate(client, writer, engine):
    csrf, kp_id = writer
    headers = {"X-CSRF-Token": csrf}
    empty_kp = client.post("/knowledge-points", json={"name": "暂无题目"}, headers=headers)
    empty_id = empty_kp.json()["data"]["id"]
    first = create(client, csrf, kp_id).json()["data"]["id"]
    second = create(client, csrf, kp_id).json()["data"]["id"]
    stats = client.get("/problems/stats").json()["data"]
    assert {row["kp_id"]: row["problem_count"] for row in stats["kp_counts"]} == {
        kp_id: 2, empty_id: 0,
    }
    assert all(row["referenced_count"] == 0 and row["correct_rate"] is None
               for row in stats["problem_references"])

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO training_plan (term, created_at) VALUES ('2026-2027-1', :now)",
            {"now": "2026-09-01 00:00:00"},
        )
        connection.exec_driver_sql(
            "INSERT INTO training_phase (plan_id, name, start_on, end_on) "
            "SELECT id, '基础', '2026-09-01', '2027-01-31' FROM training_plan "
            "WHERE term = '2026-2027-1'"
        )
        connection.exec_driver_sql(
            "INSERT INTO training_task (phase_id, title, assignee_type, status, created_at) "
            "SELECT id, '统计任务', '指定会员', '草稿', :now FROM training_phase "
            "WHERE name = '基础'",
            {"now": "2026-09-01 00:00:00"},
        )
        connection.exec_driver_sql(
            "INSERT INTO training_task (phase_id, title, assignee_type, status, created_at) "
            "SELECT id, '统计任务二', '指定会员', '草稿', :now FROM training_phase "
            "WHERE name = '基础'",
            {"now": "2026-09-01 00:00:00"},
        )
        connection.exec_driver_sql(
            "INSERT INTO task_problem (task_id, problem_id) "
            "SELECT id, ? FROM training_task WHERE title = '统计任务'", (first,)
        )
        connection.exec_driver_sql(
            "INSERT INTO task_problem (task_id, problem_id) "
            "SELECT id, ? FROM training_task WHERE title = '统计任务二'", (first,)
        )
        connection.exec_driver_sql(
            "INSERT INTO oj_submission (platform, external_id, problem_id, verdict, submitted_at, synced_at) "
            "VALUES ('codeforces', ?, ?, ?, ?, ?)",
            [(external_id, first, verdict, "2026-09-01 00:00:00", "2026-09-01 00:00:00")
             for external_id, verdict in (("s7-ac", "AC"), ("s7-wa", "WA"))],
        )
    stats = client.get("/problems/stats").json()["data"]
    usage = {row["problem_id"]: row for row in stats["problem_references"]}
    assert (usage[first]["referenced_count"], usage[first]["correct_rate"]) == (2, 0.5)
    assert (usage[second]["referenced_count"], usage[second]["correct_rate"]) == (0, None)
    html = client.get("/problems/stats", headers={"Accept": "text/html"})
    assert html.status_code == 200
    assert "暂无数据" in html.text and "50.0%" in html.text
