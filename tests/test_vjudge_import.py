from contextlib import nullcontext

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import OjSubmission, Problem
from app.models.base import local_time
from app.oj import OjError, bind_account
from app.oj import import_vjudge_csv as import_csv

HEADER = "handle,problem,verdict,submitted_at\n"


def import_vjudge_csv(session, raw):
    return import_csv(session, raw, write=nullcontext())


@pytest.fixture
def vjudge(engine, accounts):
    with SessionLocal(bind=engine) as session, session.begin():
        account = bind_account(session, accounts[0]["student01"], "vjudge", "csv_user")
        problem = Problem(
            title="导入题", platform="vjudge", external_id="HDU-1000", difficulty="入门",
            problem_type="程序", created_at=local_time(), updated_at=local_time(),
        )
        session.add(problem)
        session.flush()
        yield session, account.id, problem.id


def test_mixed_csv_preserves_good_rows_and_reports_physical_lines(vjudge):
    session, account, problem = vjudge
    result = import_vjudge_csv(session, HEADER + (
        "csv_user,HDU-1000,AC,2026-11-24T00:00:00Z\n"
        "missing,HDU-1000,AC,2026-11-24\n"
        "csv_user,HDU-1000,INVALID,2026-11-24\n"
        "csv_user,HDU-1000,AC,InvalidDate\n"
        "CSV_USER,HDU-1001,WA,2026-11-25\n"
    ))
    assert (result["accepted"], result["rejected"]) == (2, 3)
    assert [error["line"] for error in result["errors"]] == [3, 4, 5]
    assert all(error["reason"] for error in result["errors"])
    ac = session.scalar(select(OjSubmission).where(OjSubmission.verdict == "AC"))
    assert (ac.oj_account_id, ac.problem_id, ac.submitted_at) == (account, problem, "2026-11-24 08:00:00")


def test_unbound_handle_is_rejected(vjudge):
    result = import_vjudge_csv(vjudge[0], HEADER + "missing,HDU-1000,AC,2026-11-25\n")
    assert result["accepted"] == 0 and result["rejected"] == 1
    assert "未绑定" in result["errors"][0]["reason"]


@pytest.mark.parametrize("header", ["handle,verdict,problem,submitted_at", "", "x,y,z"])
def test_invalid_header_returns_parameter_error(vjudge, header):
    with pytest.raises(OjError) as raised:
        import_vjudge_csv(vjudge[0], header)
    assert raised.value.code == 40001


def test_errors_are_capped_and_malformed_quotes_do_not_swallow_good_rows(vjudge):
    result = import_vjudge_csv(vjudge[0], HEADER + '"bad\n' * 25 + "csv_user,4A,AC,2026-11-25\n")
    assert result["accepted"] == 1 and result["rejected"] == 25
    assert len(result["errors"]) == 20
    assert result["errors"][-1]["line"] == 21


def test_csv_replays_normalized_submission(vjudge):
    session, _, _ = vjudge
    good = "csv_user,HDU-1000,AC,2026-11-24T00:00:00Z\n"
    replay = "CSV_USER,HDU-1000,AC,2026-11-24 08:00:00\n"
    assert import_vjudge_csv(session, HEADER + good)["created"] == 1
    result = import_vjudge_csv(session, HEADER + replay)
    assert (result["created"], result["skipped"]) == (0, 1)
    assert len(session.scalars(select(OjSubmission)).all()) == 1


def test_csv_physical_line_limit(vjudge):
    from app.oj import MAX_CSV_LINES

    with pytest.raises(OjError, match="物理行") as raised:
        import_vjudge_csv(vjudge[0], HEADER + "\n" * MAX_CSV_LINES)
    assert raised.value.code == 40001


@pytest.mark.parametrize(("good", "bad", "exit_code"), [(1, 0, 0), (1, 25, 0), (0, 25, 1)])
def test_vjudge_cli_reports_totals_and_capped_errors(engine, accounts, tmp_path, capsys, good, bad, exit_code):
    import json

    from bin import job

    with SessionLocal(bind=engine) as session, session.begin():
        bind_account(session, accounts[0]["student01"], "vjudge", "csv_user")
    path = tmp_path / "submissions.csv"
    path.write_text(HEADER + "csv_user,HDU-1000,AC,2026-11-25\n" * good + "malformed\n" * bad)
    assert job.main(["vjudge-import", "--file", str(path)]) == exit_code
    report = json.loads(capsys.readouterr().out)
    assert (report["accepted"], report["rejected"], report["created"], report["skipped"]) == (good, bad, good, 0)
    assert len(report["errors"]) == min(bad, 20)
    assert bool(report.get("message")) == bool(good and bad)
