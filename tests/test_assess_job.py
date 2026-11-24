import json
import os
import subprocess
import sys

import pytest
from sqlalchemy import inspect, select

from app.db import SessionLocal
from app.models import JobRun, MemberAssessment


def test_s5_migration_creates_assessment_tables(engine, migration_head):
    tables = set(inspect(engine).get_table_names())
    assert {"assessment_test", "test_score", "member_assessment", "job_run"} <= tables
    indexes = inspect(engine).get_indexes("test_score")
    assert any(
        index["name"] == "uk_score" and index["unique"] and index["column_names"] == ["test_id", "member_id"]
        for index in indexes
    )
    member_assessment_indexes = inspect(engine).get_indexes("member_assessment")
    assert any(
        index["name"] == "uk_member_assessment" and index["unique"]
        and index["column_names"] == ["member_id", "term"]
        for index in member_assessment_indexes
    )
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT config_value FROM sys_config WHERE config_key = 'training.promote_threshold'"
        ).scalar_one() == "0.6"


def test_assess_term_runs_twice_keeps_one_assessment_and_job_attempts(assessment_api):
    from bin.job import assess_term, run_job

    api = assessment_api
    test_id = api["create_test"]()
    api["record"](test_id, [{"member_id": api["ids"]["student01"], "solved_count": 4, "score": 80}])

    with SessionLocal(bind=api["engine"]) as session:
        first = run_job(session, "assess", "2026-11-24",
                        lambda s: assess_term(s, "2026-2027-1"))
        second = run_job(session, "assess", "2026-11-24",
                         lambda s: assess_term(s, "2026-2027-1"))

    assert first["passed"] >= 1
    assert second["created"] == 0
    assert second["skipped"] >= 1

    with SessionLocal(bind=api["engine"]) as session:
        assert session.scalar(select(JobRun.id).where(
            JobRun.job_name == "assess", JobRun.biz_date == "2026-11-24",
        )) is not None
        runs = session.scalars(select(JobRun).where(
            JobRun.job_name == "assess", JobRun.biz_date == "2026-11-24",
        )).all()
        assert len(runs) == 1
        assert runs[0].attempts == 2
        assert runs[0].status == "success"
        rows = session.scalars(select(MemberAssessment).where(
            MemberAssessment.member_id == api["ids"]["student01"],
            MemberAssessment.term == "2026-2027-1",
        )).all()
        assert len(rows) == 1
        assert rows[0].result == "通过"


def test_run_job_failure_marks_failed_with_detail(engine):
    from bin.job import JobFailed, run_job

    def boom(_session):
        raise RuntimeError("考核计算爆炸")

    with SessionLocal(bind=engine) as session:
        with pytest.raises(JobFailed):
            run_job(session, "assess", "2026-11-25", boom)
    with SessionLocal(bind=engine) as session:
        row = session.scalar(select(JobRun).where(
            JobRun.job_name == "assess", JobRun.biz_date == "2026-11-25",
        ))
        assert row is not None
        assert row.status == "failed"
        assert json.loads(row.detail)["code"] == 50001
        assert json.loads(row.detail)["request_id"]
        assert "考核计算爆炸" not in row.detail


def test_job_cli_failure_exits_1_and_writes_failed_row(database_url, monkeypatch):
    from app.db import make_engine

    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["APP_ENV"] = "test"
    result = subprocess.run(
        [sys.executable, "bin/job.py", "assess", "--term", "bad-term"],
        cwd=".", env=env, capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert result.stderr.strip().startswith("50001 request_id=")

    engine = make_engine(database_url)
    try:
        with SessionLocal(bind=engine) as session:
            rows = session.scalars(select(JobRun).where(JobRun.job_name == "assess")).all()
            assert len(rows) == 1
            assert rows[0].status == "failed"
            assert json.loads(rows[0].detail)["code"] == 50001
    finally:
        engine.dispose()


def test_same_day_two_terms_and_retry_after_failure(assessment_api):
    from bin.job import JobFailed, assess_term, run_job

    api = assessment_api
    member_id = api["ids"]["student01"]
    test_id = api["create_test"]()
    api["record"](test_id, [{"member_id": member_id, "score": 80}])
    day = "2026-11-25"
    first, second = "2026-2027-1", "2026-2027-2"

    def fail_after_assessing(session):
        assess_term(session, first)
        raise RuntimeError("retry required")

    with SessionLocal(bind=api["engine"]) as session:
        with pytest.raises(JobFailed):
            run_job(session, "assess", day, fail_after_assessing, term=first)
        assert session.scalar(select(MemberAssessment.id).where(MemberAssessment.term == first)) is None
        run_job(session, "assess", day, lambda s: assess_term(s, second), term=second)
        result = run_job(session, "assess", day, lambda s: assess_term(s, first), term=first)
        assert result["passed"] == 1
        run_job(session, "assess", "2026-11-26", lambda s: assess_term(s, first), term=first)
        rows = session.scalars(select(JobRun).order_by(JobRun.biz_date, JobRun.term)).all()
        assert [(row.term, row.status, row.attempts) for row in rows] == [
            (first, "success", 2), (second, "success", 1), (first, "success", 1),
        ]
        assert json.loads(rows[0].detail)["passed"] == 1
        results = session.execute(select(MemberAssessment.term, MemberAssessment.result).where(
            MemberAssessment.member_id == member_id,
        )).all()
        assert dict(results) == {first: "通过", second: "不通过"}
