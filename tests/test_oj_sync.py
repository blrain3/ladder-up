import json
from pathlib import Path

from sqlalchemy import select

from app import oj
from app.db import SessionLocal
from app.models import OjSubmission

FIXTURE = Path(__file__).parent / "fixtures" / "cf_user_status.json"


def no_network(*args, **kwargs):
    raise AssertionError("OJ tests must run offline")


def test_fixture_replay_is_idempotent(engine, accounts, monkeypatch):
    monkeypatch.setenv("OJ_FIXTURE_PATH", str(FIXTURE))
    monkeypatch.setattr(oj.httpx, "AsyncClient", no_network)
    with SessionLocal(bind=engine) as session:
        owner = accounts[0]["student01"]
        account = oj.bind_account(session, owner, "codeforces", "ladder_smoke")
        session.commit()
        assert oj.sync_platform(session, "codeforces", 100) == {
            "created": 3, "skipped": 0, "errors": 0, "accounts_succeeded": 1, "error": None,
        }
        assert oj.sync_platform(session, "codeforces", 100) == {
            "created": 0, "skipped": 3, "errors": 0, "accounts_succeeded": 1, "error": None,
        }
        assert set(session.scalars(select(OjSubmission.oj_account_id))) == {account.id}
        assert set(session.scalars(select(OjSubmission.verdict))) == {"AC", "WA", "TLE"}


def test_fixture_duplicate_external_id_only_inserts_one(engine, accounts, monkeypatch, tmp_path):
    fixture = tmp_path / "duplicate.json"
    row = json.loads(FIXTURE.read_text())["result"][0]
    fixture.write_text(json.dumps({"status": "OK", "result": [row, row]}))
    monkeypatch.setenv("OJ_FIXTURE_PATH", str(fixture))
    monkeypatch.setattr(oj.httpx, "AsyncClient", no_network)
    with SessionLocal(bind=engine) as session:
        oj.bind_account(session, accounts[0]["student01"], "codeforces", "ladder_smoke")
        session.commit()
        result = oj.sync_platform(session, "codeforces", 100)
        assert (result["created"], result["skipped"]) == (1, 1)
        assert session.scalars(select(OjSubmission.external_id)).all() == ["770003"]


def test_disabled_status_never_calls_network(engine, monkeypatch):
    monkeypatch.delenv("OJ_FIXTURE_PATH", raising=False)
    monkeypatch.setattr(oj.httpx, "AsyncClient", no_network)
    with SessionLocal(bind=engine) as session:
        assert oj.fetch_user_status("ladder_smoke", "codeforces") == {"skip": "disabled"}
        assert oj.fetch_submissions("ladder_smoke", "codeforces", 100) == []
        assert oj.sync_platform(session, "codeforces", None)["skip"] == "disabled"


def test_fixture_matches_handle_and_honors_limit(monkeypatch):
    monkeypatch.setenv("OJ_FIXTURE_PATH", str(FIXTURE))
    monkeypatch.setattr(oj.httpx, "AsyncClient", no_network)
    assert oj.fetch_submissions("different", "codeforces", 2) == []
    assert [row["external_id"] for row in oj.fetch_submissions("LADDER_SMOKE", "codeforces", 2)] == [
        "770003", "770002",
    ]


def test_concurrent_syncs_insert_each_key_once(engine, accounts, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    monkeypatch.setenv("OJ_FIXTURE_PATH", str(FIXTURE))
    monkeypatch.setattr(oj.httpx, "AsyncClient", no_network)
    with SessionLocal(bind=engine) as session, session.begin():
        oj.bind_account(session, accounts[0]["student01"], "codeforces", "ladder_smoke")

    barrier = Barrier(2)
    fetch = oj.fetch_submissions

    def synchronized_fetch(*args):
        rows = fetch(*args)
        barrier.wait(timeout=5)
        return rows

    def sync():
        with SessionLocal(bind=engine) as session:
            result = oj.sync_platform(session, "codeforces", 100)
            return result["created"], result["skipped"]

    monkeypatch.setattr(oj, "fetch_submissions", synchronized_fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        workers = [pool.submit(sync) for _ in range(2)]
        assert sorted(worker.result(timeout=10) for worker in workers) == [(0, 3), (3, 0)]
    with SessionLocal(bind=engine) as session:
        assert sorted(session.scalars(select(OjSubmission.external_id))) == ["770001", "770002", "770003"]
