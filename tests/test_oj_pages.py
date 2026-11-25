import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from test_oj_sync import FIXTURE, no_network
from test_vjudge_import import HEADER

from app import oj
from app.db import SessionLocal
from app.models import AssessmentTest, AuditLog, OjSubmission, Problem, TestScore
from app.models.base import local_time

HTML = {"Accept": "text/html"}


@pytest.fixture
def oj_flow(client, signed_in, coach, accounts, engine, monkeypatch):
    monkeypatch.setenv("OJ_FIXTURE_PATH", str(FIXTURE))
    monkeypatch.setattr(oj.httpx, "AsyncClient", no_network)
    owner = accounts[0]["student01"]
    with SessionLocal(bind=engine) as session, session.begin():
        oj.bind_account(session, owner, "codeforces", "ladder_smoke")
        oj.bind_account(session, owner, "vjudge", "csv_user")
        test = AssessmentTest(
            title="测评", test_type="周测", held_on=datetime.now(oj.SHANGHAI).date().isoformat(),
            created_at=local_time(),
        )
        session.add(test)
        session.add(Problem(
            platform="codeforces", external_id="4A", title="CF 样例题", difficulty="入门",
            problem_type="程序", created_at=local_time(), updated_at=local_time(),
        ))
        session.flush()
        session.add(TestScore(
            test_id=test.id, member_id=owner, kp_snapshot='{"最短路":{"correct":1,"total":4}}',
        ))

    coach_token = signed_in(client, "coach0001")
    csv = HEADER + "csv_user,HDU-1000,AC,2026-11-24\ncsv_user,HDU-1000,BAD,2026-11-24\n"
    imported = client.post("/oj/vjudge-import", files={"file": ("records.csv", csv, "text/csv")},
                           data={"csrf_token": coach_token})
    report = imported.json()["data"]
    assert (report["accepted"], report["rejected"]) == (1, 1)
    started = client.post("/oj/sync", json={}, headers={"X-CSRF-Token": coach_token})
    assert started.status_code == 202
    url = f"/jobs/{started.json()['data']['jobRunId']}"
    assert started.headers["Location"] == url
    job = client.get(url).json()["data"]
    assert job["status"] == "success" and job["detail"]["created"] == 3
    return client, signed_in, url, owner, coach_token


@pytest.mark.parametrize(("path", "actor", "content"), [
    ("/oj/accounts", "student01", ("ladder_smoke", "绑定", "OJ 同步")),
    ("/oj/submissions", "student01", ("770003", 'class="badge text-bg-success">AC', "CF 样例题")),
    ("/oj/vjudge-import", "coach0001", ("UTF-8 CSV 文件", "handle,problem,verdict,submitted_at", 'type="file"', 'name="file"')),
    ("/oj/sync", "coach0001", ("触发同步", "查看最近同步")),
    ("job", "coach0001", ("作业状态", "成功", "3 / 0")),
    ("/training-advice", "student01", ("知识点「最短路」正确率偏低", "依据", "1 / 4 题", "25.00%")),
])
def test_six_html_pages_show_real_content(oj_flow, path, actor, content):
    client, login, job_url, _, _ = oj_flow
    login(client, actor)
    response = client.get(job_url if path == "job" else path, headers=HTML)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert all(text in response.text for text in content)


def test_import_report_and_errors_are_visible_in_html(oj_flow):
    client, _, _, _, coach_token = oj_flow
    response = client.post(
        "/oj/vjudge-import", files={"file": ("records.csv", HEADER + "csv_user,4A,BAD,2026-11-24\n")},
        data={"csrf_token": coach_token}, headers=HTML,
    )
    assert response.status_code == 200
    assert "<td>2</td>" in response.text and "verdict 不在允许集合" in response.text
    invalid = client.post("/oj/vjudge-import", files={"file": ("bad.csv", "bad header")},
                          data={"csrf_token": coach_token}, headers=HTML)
    assert invalid.status_code == 400 and "表头须为" in invalid.text


def test_member_access_is_scoped_and_management_operations_are_denied(oj_flow, accounts):
    client, login, job_url, owner, _ = oj_flow
    other_token = login(client, "student02")
    assert client.get("/oj/submissions").json()["data"]["items"] == []
    assert client.get("/training-advice").json()["data"] == []
    denied = [f"/oj/submissions?member_id={owner}", f"/training-advice?member_id={owner}", job_url]
    for url in denied + ["/oj/vjudge-import", "/oj/sync"]:
        assert client.get(url).status_code == 403
    for url in ("/oj/vjudge-import", "/oj/sync"):
        assert client.post(url, json={}, headers={"X-CSRF-Token": other_token}).status_code == 403
    html = client.get("/oj/accounts", headers=HTML).text
    assert "暂无数据" in html and "导入 Vjudge" not in html and "ladder_smoke" not in html
    login(client, "coach0001")
    assert client.get(f"/training-advice?member_id={owner}").json()["data"][0]["basis"]["total"] == 4
    assert client.get(f"/training-advice?member_id={accounts[0]['student02']}").json()["data"] == []


def test_submissions_paginate_filter_and_keep_audit(oj_flow, engine):
    client, _, _, owner, _ = oj_flow
    path = "/oj/submissions?platform=codeforces&page_size=1"
    first = client.get(path).json()["data"]
    assert first["total"] == 3 and first["items"][0]["external_id"] == "770003"
    next_page = client.get(first["links"]["next"], headers=HTML)
    assert "770002" in next_page.text and "上一页" in next_page.text
    with SessionLocal(bind=engine) as session, session.begin():
        account_id = session.scalar(select(oj.OjAccount.id).where(oj.OjAccount.platform == "vjudge"))
        session.add(OjSubmission(
            platform="vjudge", external_id="out-of-range", oj_account_id=account_id,
            verdict="AC", submitted_at="2026-11-25 00:00:00", synced_at=local_time(),
        ))
    filtered = client.get("/oj/submissions?platform=vjudge&verdict=AC&date_from=2026-11-24&date_to=2026-11-24")
    row, = filtered.json()["data"]["items"]
    assert row["member_id"] == owner and row["verdict"] == "AC"
    invalid_path = "/oj/submissions?date_from=2026-12-01&date_to=2026-01-01"
    assert client.get(invalid_path).json()["code"] == 40001
    invalid = client.get(invalid_path, headers=HTML)
    assert invalid.status_code == 400 and "起始日期须早于或等于结束日期" in invalid.text
    with SessionLocal(bind=engine) as session:
        actions = set(session.scalars(select(AuditLog.action)))
        assert {"oj.vjudge.imported", "oj.sync.requested", "oj.sync.completed"} <= actions
        imported = session.scalar(select(AuditLog).where(AuditLog.action == "oj.vjudge.imported"))
        assert json.loads(imported.after_json) == {"accepted": 1, "rejected": 1, "created": 1, "skipped": 0}


def test_sync_http_reports_running_job_conflict(oj_flow, engine):
    from app.models import JobRun

    client, _, url, _, coach_token = oj_flow
    with SessionLocal(bind=engine) as session, session.begin():
        row = session.get(JobRun, int(url.rsplit("/", 1)[1]))
        row.status = "running"
        row.lease_expires_at = (datetime.now(oj.SHANGHAI) + timedelta(minutes=30)).isoformat(sep=" ")
    response = client.post("/oj/sync", json={}, headers={"X-CSRF-Token": coach_token})
    assert response.status_code == 409 and response.json()["code"] == 40902


def test_dept_admin_scope_survives_explicit_approval_grant(oj_flow, engine, accounts):
    from app.models import MemberPermission, PermissionPoint

    client, login, job_url, owner, _ = oj_flow
    ids = accounts[0]
    with SessionLocal(bind=engine) as session, session.begin():
        cross = oj.bind_account(session, ids["student02"], "vjudge", "outside")
        session.add(OjSubmission(
            platform="vjudge", external_id="outside", oj_account_id=cross.id, verdict="AC",
            submitted_at=local_time(), synced_at=local_time(),
        ))
        approval = session.scalar(select(PermissionPoint).where(PermissionPoint.code == "training:approve"))
        if approval is None:
            approval = PermissionPoint(code="training:approve", module="B", description="训练审批")
            session.add(approval)
            session.flush()
        session.add(MemberPermission(
            member_id=ids["deptadmin"], permission_id=approval.id, granted_at=local_time(),
        ))
    csrf = login(client, "deptadmin")
    assert client.get(f"/oj/accounts?member_id={owner}").json()["data"]["items"]
    assert client.get(f"/oj/accounts?member_id={ids['student02']}").status_code == 403
    visible = client.get("/oj/submissions").json()["data"]["items"]
    assert len(visible) == 4 and {item["member_id"] for item in visible} == {owner}
    assert client.get(f"/oj/submissions?member_id={owner}").json()["data"]["total"] == 4
    assert client.get(f"/oj/submissions?member_id={ids['student02']}").status_code == 403
    for path in ("/oj/sync", "/oj/vjudge-import", job_url, "/oj/sync-tasks/" + job_url.split("/")[-1]):
        assert client.get(path).status_code == 403
    for path in ("/oj/sync", "/oj/sync-submissions", "/oj/vjudge-import"):
        assert client.post(path, json={}, headers={"X-CSRF-Token": csrf}).status_code == 403
    assert "导入 Vjudge" not in client.get("/oj/accounts", headers=HTML).text
    login(client, "coach0001")
    assert client.get("/oj/submissions").json()["data"]["total"] == 5


@pytest.mark.parametrize("declared_length", [None, "1"])
def test_upload_enforces_declared_and_actual_body_limits(oj_flow, monkeypatch, declared_length):
    client, _, _, _, coach_token = oj_flow
    from app import routes

    monkeypatch.setattr(routes, "MAX_CSV_BYTES", 1024)
    headers = {"Content-Length": declared_length} if declared_length is not None else {}
    oversized = client.post("/oj/vjudge-import", files={"file": ("big.csv", b"x" * 1024)},
                            data={"csrf_token": coach_token}, headers=headers)
    assert oversized.status_code == 400 and oversized.json()["code"] == 40001
    assert "上传请求不能超过 5 MiB" in oversized.json()["message"]


def test_another_connection_can_check_in_during_csv_parsing(training_api, client_factory, signed_in, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    api = training_api
    task = api["task"](api["phase"](api["plan"]()), api["problems"]("并发打卡题"))
    api["assign"](task, "指定会员", member_ids=[api["ids"]["student01"]])
    with SessionLocal(bind=api["engine"]) as session, session.begin():
        oj.bind_account(session, api["ids"]["student01"], "vjudge", "csv_user")
    member_client = client_factory()
    member_csrf = signed_in(member_client)
    parsing = Event()
    resume = Event()
    original = oj.parse_vjudge_csv

    def pause_parser(raw):
        parsing.set()
        assert resume.wait(timeout=10)
        return original(raw)

    def import_csv():
        return api["client"].post(
            "/oj/vjudge-import", files={"file": ("records.csv", HEADER + "csv_user,HDU-1000,AC,2026-11-24\n")},
            data={"csrf_token": api["coach_csrf"]},
        )

    monkeypatch.setattr(oj, "parse_vjudge_csv", pause_parser)
    with ThreadPoolExecutor(max_workers=2) as pool:
        imported = pool.submit(import_csv)
        try:
            assert parsing.wait(timeout=5)
            checkin = pool.submit(
                member_client.post, "/checkins", json={"task_id": task, "solved_count": 0},
                headers={"X-CSRF-Token": member_csrf},
            ).result(timeout=3)
            assert checkin.status_code == 200, checkin.text
        finally:
            resume.set()
        report = imported.result(timeout=5)
    assert report.status_code == 200 and report.json()["data"]["created"] == 1
