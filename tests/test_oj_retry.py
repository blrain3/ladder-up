import asyncio
import json
import time
from functools import partial

import httpx
import pytest
from sqlalchemy import select, text
from test_oj_sync import FIXTURE, no_network

from app import oj
from app.db import SessionLocal
from app.models import AuditLog, JobRun, OjSubmission, SysConfig
from bin import job


def configure(engine, accounts, monkeypatch, *, second=False):
    monkeypatch.delenv("OJ_FIXTURE_PATH", raising=False)
    with SessionLocal(bind=engine) as session, session.begin():
        session.get(SysConfig, "oj.sync.enabled").config_value = "true"
        session.get(SysConfig, "oj.sync.limit").config_value = "2"
        oj.bind_account(session, accounts[0]["student01"], "codeforces", "ladder_smoke")
        if second:
            oj.bind_account(session, accounts[0]["student02"], "codeforces", "slow_user")


class ResponseStream(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body

    async def __aiter__(self):
        yield self.body


def mock_transport(monkeypatch, handler):
    client = partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(oj.httpx, "AsyncClient", client)


def record_requests(monkeypatch, *, failures, second_only=False, engine=None):
    clock = [100.0]
    calls = []
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
        if engine is not None:
            with SessionLocal(bind=engine) as session:
                session.execute(text("BEGIN IMMEDIATE"))
                session.execute(text("UPDATE sys_config SET config_value = '2' WHERE config_key = 'oj.sync.limit'"))
                session.commit()

    def response(request):
        url = str(request.url)
        calls.append((url, clock[0]))
        if (second_only and "slow_user" in url) or (not second_only and len(calls) <= failures):
            return httpx.Response(429)
        return httpx.Response(200, stream=ResponseStream(FIXTURE.read_bytes()))

    monkeypatch.setattr(oj, "monotonic", lambda: clock[0])
    monkeypatch.setattr(oj, "sleep", sleep)
    mock_transport(monkeypatch, response)
    return calls, sleeps


def test_two_429_retries_recover_with_request_spacing(engine, accounts, monkeypatch):
    configure(engine, accounts, monkeypatch)
    calls, sleeps = record_requests(monkeypatch, failures=2)
    with SessionLocal(bind=engine) as session:
        result = job.run_oj_job(session)
        run = session.scalar(select(JobRun))
        assert run.status == "success" and result["created"] == 2
    assert len(calls) == 3 and sleeps == [2, 2]
    assert all("count=2" in url for url, _ in calls)
    assert all(b[1] - a[1] >= 2 for a, b in zip(calls, calls[1:], strict=False))


def test_exhausted_429_records_failure_and_preserves_previous_account(
    engine, accounts, monkeypatch, capsys,
):
    configure(engine, accounts, monkeypatch, second=True)
    calls, sleeps = record_requests(monkeypatch, failures=4, second_only=True, engine=engine)
    assert job.main(["oj-sync", "--platform", "codeforces", "--limit", "1"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "created": 1, "skipped": 0, "errors": 1, "accounts_succeeded": 1, "error": "429",
    }
    assert len(calls) == 5 and sleeps == [2, 2, 2, 4]
    with SessionLocal(bind=engine) as session:
        row = session.scalar(select(JobRun))
        assert row.status == "failed" and "429" in row.detail and row.finished_at
        assert session.scalars(select(OjSubmission.external_id)).all() == ["770003"]


def test_disabled_cli_marks_skipped_and_same_day_reuses_row(engine, monkeypatch, capsys):
    monkeypatch.delenv("OJ_FIXTURE_PATH", raising=False)
    monkeypatch.setattr(oj.httpx, "AsyncClient", no_network)
    for _ in range(2):
        assert job.main(["oj-sync", "--platform", "codeforces"]) == 0
        assert json.loads(capsys.readouterr().out)["skip"] == "disabled"
    with SessionLocal(bind=engine) as session:
        row, = session.scalars(select(JobRun)).all()
        assert row.status == "skipped" and row.attempts == 2
        assert json.loads(row.detail)["skip"] == "disabled"


def test_running_job_rejects_concurrent_attempt(engine, monkeypatch, capsys):
    monkeypatch.setattr(oj.httpx, "AsyncClient", no_network)
    with SessionLocal(bind=engine) as session:
        job.reserve_job(session, job.OJ_JOB, job.oj_job_date(), exclusive=True)
        session.commit()
    assert job.main(["oj-sync", "--platform", "codeforces"]) == 1
    assert "40902" in capsys.readouterr().err
    with SessionLocal(bind=engine) as session:
        row, = session.scalars(select(JobRun)).all()
        assert row.status == "running" and row.attempts == 1


@pytest.mark.parametrize(("body", "code"), [
    (b"<html>error</html>", "INVALID_RESPONSE"),
    (b"[" * 2000 + b"0" + b"]" * 2000, "INVALID_RESPONSE"),
    (b"x" * (oj.MAX_RESPONSE_BYTES + 1), "RESPONSE_TOO_LARGE"),
    (b'{"result":[]}', "INVALID_RESPONSE"),
    (b'{"status":"OK"}', "INVALID_RESPONSE"),
    (b'{"status":"OK","result":[NaN]}', "INVALID_RESPONSE"),
])
def test_malformed_http_response_is_bounded_and_classified(monkeypatch, body, code):
    clock = [100.0]
    monkeypatch.setattr(oj, "monotonic", lambda: clock[0])
    monkeypatch.setattr(oj, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    mock_transport(monkeypatch, lambda _: httpx.Response(200, stream=ResponseStream(body)))
    with pytest.raises(oj.FetchError, match=f"^{code}$"):
        oj.CodeforcesRequests(True, 100).fetch("ladder_smoke")


def test_slow_drip_hits_total_deadline(monkeypatch):
    closed = []

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.01)
                yield b"x"

        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr(oj, "FETCH_SECONDS", 0.08)
    mock_transport(monkeypatch, lambda _: httpx.Response(200, stream=SlowBody()))
    started = time.monotonic()
    with pytest.raises(oj.FetchError, match="^FETCH_TIMEOUT$"):
        oj.CodeforcesRequests(True, 100).fetch("ladder_smoke")
    assert closed == [True]
    assert time.monotonic() - started < 1


def test_missing_fields_and_long_author_are_excluded_without_losing_valid_rows(
    engine, accounts, monkeypatch, tmp_path,
):
    configure(engine, accounts, monkeypatch)
    good = json.loads(FIXTURE.read_text())["result"][0]
    malformed = []
    for field in ("author", "problem", "verdict", "creationTimeSeconds", "id"):
        malformed.append({key: value for key, value in good.items() if key != field})
    malformed.extend((
        {**good, "author": {"members": [{"handle": "x" * 81}]}},
        {**good, "problem": {"contestId": 4}},
        {**good, "verdict": "UNKNOWN_VERDICT"},
        {**good, "creationTimeSeconds": True},
    ))
    fixture = tmp_path / "bad-rows.json"
    fixture.write_text(json.dumps({"status": "OK", "result": [*malformed, good]}))
    monkeypatch.setenv("OJ_FIXTURE_PATH", str(fixture))
    assert oj.fetch_submissions("ladder_smoke", "codeforces", 2)[:-1] == [
        {"error": "INVALID_SUBMISSION"},
    ] * len(malformed)
    with SessionLocal(bind=engine) as session:
        result = job.run_oj_job(session)
        assert (result["created"], result["errors"], result["error"]) == (1, 1, "INVALID_SUBMISSION")
        assert session.scalars(select(OjSubmission.external_id)).all() == ["770003"]


def test_middle_account_failure_allows_later_account_to_commit(engine, accounts, monkeypatch, capsys):
    configure(engine, accounts, monkeypatch, second=True)
    with SessionLocal(bind=engine) as session, session.begin():
        oj.bind_account(session, accounts[0]["deptadmin"], "codeforces", "later_user")

    def fetch(handle, platform, limit):
        if handle == "slow_user":
            raise oj.FetchError("FETCH_TIMEOUT")
        return [{"external_id": handle, "problem": "4A", "verdict": "AC",
                 "submitted_at": "2026-11-25 00:00:00"}]

    monkeypatch.setattr(oj, "fetch_submissions", fetch)
    assert job.main(["oj-sync"]) == 1
    assert json.loads(capsys.readouterr().out)["errors"] == 1
    with SessionLocal(bind=engine) as session:
        row = session.scalar(select(JobRun))
        assert row.status == "failed"
        assert json.loads(row.detail)["message"] == "成功 2 个账号、失败 1 个账号、最后错误 FETCH_TIMEOUT"
        assert set(session.scalars(select(OjSubmission.external_id))) == {"ladder_smoke", "later_user"}


@pytest.mark.parametrize("same_day", [False, True])
def test_expired_lease_takeover_blocks_stale_finalization(engine, monkeypatch, same_day):
    first_day = "2026-11-25"
    second_day = first_day if same_day else "2026-11-26"
    clock = ["2026-11-25 23:55:00"]
    monkeypatch.setattr(job, "local_time", lambda: clock[0])
    with SessionLocal(bind=engine) as session:
        stale = job.reserve_job(session, job.OJ_JOB, first_day, exclusive=True)
        session.commit()
    with SessionLocal(bind=engine) as session:
        with pytest.raises(job.JobRunning):
            job.reserve_job(session, job.OJ_JOB, second_day, exclusive=True)
    clock[0] = "2026-11-26 00:25:00"
    with SessionLocal(bind=engine) as session:
        current = job.reserve_job(session, job.OJ_JOB, second_day, exclusive=True)
        session.commit()
        if same_day:
            assert (current.id, current.attempt) == (stale.id, 2)
        else:
            old = session.get(JobRun, stale.id)
            assert (old.status, old.detail) == ("failed", "超时接管")

    with SessionLocal(bind=engine) as session:
        with pytest.raises(job.JobFailed):
            job.run_job(session, job.OJ_JOB, first_day, lambda _: {}, reservation=stale)
        assert session.get(JobRun, current.id).status == "running"
        job.run_job(session, job.OJ_JOB, second_day, lambda _: {}, reservation=current)
        assert session.get(JobRun, current.id).status == "success"
        audit = session.scalar(select(AuditLog).where(AuditLog.action == "oj.sync.lease_expired"))
        assert audit.target_id == stale.id and "超时接管" in audit.after_json
