def test_six_training_pages_render_with_key_copy(training_api):
    api = training_api
    client = api["client"]
    pages = [
        ("/training-plans", ["训练计划", "暂无数据"]),
        ("/training-phases", ["训练阶段", "暂无数据"]),
        ("/training-tasks", ["训练任务", "暂无数据"]),
        ("/checkins", ["打卡", "暂无数据"]),
        ("/training-progress", ["完成率"]),
        ("/training-alerts", ["训练预警", "暂无数据"]),
    ]
    for path, snippets in pages:
        response = client.get(path, headers={"Accept": "text/html"})
        assert response.status_code == 200, (path, response.status_code, response.text)
        assert "text/html" in response.headers["Content-Type"]
        for snippet in snippets:
            assert snippet in response.text, (path, snippet)


def test_task_list_and_detail_pages_show_publish_and_progress_copy(training_api):
    api = training_api
    client = api["client"]
    plan_id = api["plan"]()
    phase_id = api["phase"](plan_id)
    problem_ids = api["problems"]("P1")
    task_id = api["task"](phase_id, problem_ids)

    response = client.get("/training-tasks", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert "下发" in response.text

    detail = client.get(f"/training-tasks/{task_id}", headers={"Accept": "text/html"})
    assert detail.status_code == 200
    assert "text/html" in detail.headers["Content-Type"]
    assert "周任务" in detail.text
    assert "题目列表" in detail.text
    assert "下发" in detail.text
    assert "完成率" in detail.text


def test_training_pages_show_member_data(training_api):
    from datetime import date, timedelta

    from app.db import SessionLocal
    from app.models import TrainingTask
    from app.models.base import local_time

    api = training_api
    phase_id = api["phase"](api["plan"]())
    problem_ids = api["problems"]("训练样题甲", "训练样题乙")
    task_id = api["task"](phase_id, problem_ids, title="两题练习")
    api["assign"](task_id, "指定会员", member_ids=[api["ids"]["student01"], api["ids"]["student02"]])
    api["checkin"]("student01", task_id, solved_count=1, solved_problem_ids=problem_ids[:1])
    for url, snippets in (
        (f"/checkins?task_id={task_id}", ("训练样题甲", "提交打卡")),
        (f"/training-tasks/{task_id}", ("两题练习",)),
        ("/training-progress", ("1 / 2",)),
    ):
        response = api["client"].get(url, headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert "text/html" in response.headers["Content-Type"]
        assert all(snippet in response.text for snippet in snippets)
        assert "student02" not in response.text
    details = api["client"].get(f"/training-tasks/{task_id}").json()["data"]
    assert [row["student_no"] for row in details["assignees"]] == ["student01"]
    listing = api["client"].get("/training-tasks", headers={"Accept": "text/html"})
    assert "创建任务" not in listing.text and "指定会员（可多选）" not in listing.text

    with SessionLocal(bind=api["engine"]) as session, session.begin():
        yesterday = date.fromisoformat(local_time()[:10]) - timedelta(days=1)
        session.get(TrainingTask, task_id).published_at = f"{yesterday} 09:00:00"
    api["member_csrf"]("coach0001")
    warning = api["client"].get("/training-alerts", headers={"Accept": "text/html"})
    assert warning.status_code == 200
    assert "text/html" in warning.headers["Content-Type"]
    assert "student02" in warning.text and "<td>2</td>" in warning.text
