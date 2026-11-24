"""S5 定时任务 CLI：考核计算与任务运行记录查询.

用法：
    python bin/job.py assess --term 2026-2027-1
    python bin/job.py runs --limit 20

run_job 以 (job_name, biz_date, term) 幂等：重复运行不新建 job_run 行，attempts 自增；
fn 抛错时置 status=failed、detail 保存错误码和请求标识，CLI 以退出码 1 结束。
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from traceback import extract_tb
from uuid import uuid4
from zoneinfo import ZoneInfo

# Support direct execution as python bin/job.py.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert

from app.assessment import assess_member, term_interval
from app.config import Settings
from app.db import SessionLocal, make_engine
from app.models import JobRun, Member, MemberAssessment
from app.models.base import local_time
from app.training import active_members_query

SHANGHAI = ZoneInfo("Asia/Shanghai")


def assess_term(session, term: str) -> dict:
    """产出 member_assessment，返回 {created, skipped, passed, failed_count}。"""
    term_interval(term)
    created = 0
    skipped = 0
    passed = 0
    failed_count = 0
    members = session.scalars(active_members_query().order_by(Member.id))
    for member in members:
        # Existing rows are counted separately. The upsert enforces uniqueness.
        assessment = session.scalar(select(MemberAssessment.id).where(
            MemberAssessment.member_id == member.id, MemberAssessment.term == term,
        ))
        result = assess_member(session, member.id, term)
        if assessment is None:
            created += 1
        else:
            skipped += 1
        if result == "通过":
            passed += 1
        else:
            failed_count += 1
    return {"created": created, "skipped": skipped, "passed": passed,
            "failed_count": failed_count}


class JobFailed(Exception):
    def __init__(self, request_id):
        self.request_id = request_id
        super().__init__(f"50001 request_id={request_id}")


def log_job_failure(request_id, exc):
    # SQL and exception text can contain score values or personal information.
    locations = " > ".join(f"{Path(frame.filename).name}:{frame.lineno}" for frame in extract_tb(exc.__traceback__))
    logging.getLogger(__name__).error("Job failure request_id=%s exception=%s locations=%s",
                                      request_id, type(exc).__name__, locations)


def run_job(session, job_name: str, biz_date: str, fn, *, term="", request_id=None) -> dict:
    """Hold the SQLite write lock through the attempt and commit its terminal state."""
    request_id = request_id or uuid4().hex
    try:
        now = local_time()
        row_id = session.execute(insert(JobRun).values(
            job_name=job_name, biz_date=biz_date, term=term, started_at=now, status="running",
        ).on_conflict_do_update(
            index_elements=["job_name", "biz_date", "term"],
            set_={"attempts": JobRun.attempts + 1, "started_at": now, "status": "running"},
        ).returning(JobRun.id)).scalar_one()
        failure = None
        try:
            with session.begin_nested():
                result = fn(session)
                detail = json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            failure = exc
            detail = json.dumps({"code": 50001, "request_id": request_id})
        session.execute(update(JobRun).where(JobRun.id == row_id).values(
            status="failed" if failure else "success", detail=detail, finished_at=local_time(),
        ))
        session.commit()
        if failure is not None:
            raise failure
        return result
    except Exception as exc:
        session.rollback()
        log_job_failure(request_id, exc)
        raise JobFailed(request_id) from None


def runs(session, limit):
    return session.scalars(
        select(JobRun).order_by(JobRun.started_at.desc(), JobRun.id.desc()).limit(limit)
    ).all()


def build_parser():
    parser = argparse.ArgumentParser(prog="bin/job.py")
    subparsers = parser.add_subparsers(dest="command", required=True)
    assess = subparsers.add_parser("assess", help="计算指定学期的会员考核结果")
    assess.add_argument("--term", required=True, help="如 2026-2027-1")
    runs_parser = subparsers.add_parser("runs", help="查看最近任务运行记录")
    runs_parser.add_argument("--limit", type=int, default=20)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    logger = logging.getLogger(__name__)
    engine = handler = None
    try:
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        handler = logging.FileHandler(log_dir / "app.log", encoding="utf-8")
        logger.addHandler(handler)
        engine = make_engine(Settings.from_env().database_url)
        with SessionLocal(bind=engine) as session:
            if args.command == "assess":
                biz_date = datetime.now(SHANGHAI).date().isoformat()
                result = run_job(session, "assess", biz_date,
                                 lambda s: assess_term(s, args.term), term=args.term)
                print(json.dumps(result, ensure_ascii=False))
            elif args.command == "runs":
                rows = runs(session, args.limit)
                for row in rows:
                    print(
                        f"{row.id}\t{row.job_name}\t{row.biz_date}\t{row.term}\t{row.status}"
                        f"\tattempts={row.attempts}\t{row.started_at}\t"
                        f"{(row.finished_at or '')}\t{(row.detail or '')}"
                    )
    except JobFailed as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        request_id = uuid4().hex
        if handler:
            log_job_failure(request_id, exc)
        print(str(JobFailed(request_id)), file=sys.stderr)
        return 1
    finally:
        if handler:
            logger.removeHandler(handler)
            handler.close()
        if engine:
            engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
