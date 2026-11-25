"""定时任务 CLI：考核计算与任务运行记录查询.

用法：
    python bin/job.py assess --term 2026-2027-1
    python bin/job.py oj-sync --platform codeforces
    python bin/job.py runs --limit 20

run_job 以 (job_name, biz_date, term) 幂等：重复运行不新建 job_run 行，attempts 自增；
fn 抛错时置 status=failed、detail 保存错误码和请求标识，CLI 以退出码 1 结束。
"""

import argparse
import json
import logging
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from traceback import extract_tb
from uuid import uuid4
from zoneinfo import ZoneInfo

# Support direct execution as python bin/job.py.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import or_, select, text, update
from sqlalchemy.dialects.sqlite import insert

from app.assessment import assess_member, term_interval
from app.config import Settings
from app.db import SessionLocal, make_engine
from app.models import AuditLog, JobRun, Member, MemberAssessment
from app.models.base import local_time
from app.oj import MAX_CSV_BYTES, OjError, import_vjudge_csv, sync_platform, validated_limit
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


class JobRunning(Exception):
    pass


@dataclass(frozen=True)
class JobReservation:
    id: int
    attempt: int


def lease_deadline(now):
    return (datetime.fromisoformat(now) + timedelta(minutes=30)).isoformat(sep=" ")


def reserve_job(session, job_name, biz_date, *, term="", exclusive=False):
    now = local_time()
    lease = None
    if exclusive:
        session.execute(text("BEGIN IMMEDIATE"))
        now = local_time()
        lease = lease_deadline(now)
        running = update(JobRun).where(JobRun.job_name == job_name, JobRun.status == "running")
        expired_ids = session.scalars(running.where(or_(
            JobRun.lease_expires_at.is_(None), JobRun.lease_expires_at <= now,
        )).values(status="failed", detail="超时接管", finished_at=now, lease_expires_at=None)
            .returning(JobRun.id)).all()
        if session.scalar(select(JobRun.id).where(
            JobRun.job_name == job_name, JobRun.status == "running",
        ).limit(1)) is not None:
            raise JobRunning("已有实例在运行")
        for expired_id in expired_ids:
            session.add(AuditLog(
                request_id=uuid4().hex, action="oj.sync.lease_expired", target_type="job_run",
                target_id=expired_id, created_at=now, after_json=json.dumps({"detail": "超时接管"}, ensure_ascii=False),
            ))

    row_id, attempt = session.execute(insert(JobRun).values(
        job_name=job_name, biz_date=biz_date, term=term, started_at=now, status="running",
        lease_expires_at=lease,
    ).on_conflict_do_update(
        index_elements=["job_name", "biz_date", "term"],
        set_={"attempts": JobRun.attempts + 1, "started_at": now, "status": "running",
              "detail": None, "finished_at": None, "lease_expires_at": lease},
    ).returning(JobRun.id, JobRun.attempts)).one()
    return JobReservation(row_id, attempt)


def running_reservation(reservation):
    return update(JobRun).where(
        JobRun.id == reservation.id, JobRun.status == "running", JobRun.attempts == reservation.attempt,
    )


def renew_job_lease(session, reservation):
    now = local_time()
    renewed = session.execute(running_reservation(reservation).where(
        JobRun.lease_expires_at > now,
    ).values(lease_expires_at=lease_deadline(now)))
    if not renewed.rowcount:
        raise JobRunning("作业租约已失效")


def run_job(session, job_name: str, biz_date: str, fn, *, term="", request_id=None,
            reservation=None, actor_id=None) -> dict:
    """Run atomically by default. A committed reservation permits incremental writes."""
    request_id = request_id or uuid4().hex
    try:
        run = reservation or reserve_job(
            session, job_name, biz_date, term=term,
        )
        failure = None
        try:
            if reservation is None:
                with session.begin_nested():
                    result = fn(session)
                    detail = json.dumps(result, ensure_ascii=False)
            else:
                result = fn(session)
                detail = json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            if reservation is not None:
                session.rollback()
            failure = exc
            result = {"code": 50001, "request_id": request_id}
            detail = json.dumps(result)

        status = "success"
        if failure is not None or (reservation is not None and result.get("error")):
            status = "failed"
            if failure is None:
                detail = json.dumps({
                    **result, "message": f"成功 {result['accounts_succeeded']} 个账号、"
                                        f"失败 {result['errors']} 个账号、最后错误 {result['error']}",
                }, ensure_ascii=False)
        elif reservation is not None and result.get("skip"):
            status = "skipped"
        finished_at = local_time()
        statement = running_reservation(run)
        if reservation is not None:
            statement = statement.where(JobRun.lease_expires_at > finished_at)
        finished = session.execute(statement.values(
            status=status, detail=detail, finished_at=finished_at, lease_expires_at=None,
        ))
        if not finished.rowcount:
            raise JobRunning("作业租约已失效")
        if reservation is not None:
            session.add(AuditLog(
                request_id=request_id, actor_id=actor_id, action="oj.sync.completed",
                target_type="job_run", target_id=run.id, created_at=local_time(), after_json=detail,
            ))
        session.commit()
        if failure is not None:
            raise failure
        return result
    except Exception as exc:
        session.rollback()
        log_job_failure(request_id, exc)
        raise JobFailed(request_id) from None


OJ_JOB = "oj.sync"


def oj_job_date():
    return datetime.now(SHANGHAI).date().isoformat()


def run_oj_job(session, limit=None, *, reservation=None, request_id=None, actor_id=None):
    if limit is not None:
        limit = validated_limit(limit)
    biz_date = oj_job_date()
    if reservation is None:
        reservation = reserve_job(session, OJ_JOB, biz_date, exclusive=True)
        session.commit()
    return run_job(
        session, OJ_JOB, biz_date, lambda s: sync_platform(
            s, "codeforces", limit, renew_lease=lambda locked: renew_job_lease(locked, reservation),
        ),
        reservation=reservation, request_id=request_id, actor_id=actor_id,
    )


def runs(session, limit):
    return session.scalars(
        select(JobRun).order_by(JobRun.started_at.desc(), JobRun.id.desc()).limit(limit)
    ).all()


def build_parser():
    parser = argparse.ArgumentParser(prog="bin/job.py")
    subparsers = parser.add_subparsers(dest="command", required=True)
    assess = subparsers.add_parser("assess", help="计算指定学期的会员考核结果")
    assess.add_argument("--term", required=True, help="如 2026-2027-1")
    oj_sync = subparsers.add_parser("oj-sync", help="同步外部 OJ 提交记录")
    oj_sync.add_argument("--platform", default="codeforces", choices=("codeforces",), help="平台（默认 codeforces）")
    oj_sync.add_argument("--limit", type=int, default=None, help="单账号拉取上限，默认读 oj.sync.limit")
    vjudge = subparsers.add_parser("vjudge-import", help="导入 Vjudge CSV")
    vjudge.add_argument("--file", type=Path, required=True)
    runs_parser = subparsers.add_parser("runs", help="查看最近任务运行记录")
    runs_parser.add_argument("--limit", type=int, default=20)
    return parser


@contextmanager
def vjudge_write(session):
    with session.begin():
        session.execute(text("BEGIN IMMEDIATE"))
        yield


def import_vjudge_file(session, path):
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CSV_BYTES + 1)
    except OSError:
        raise OjError("无法读取 CSV 文件") from None

    report = import_vjudge_csv(session, raw, write=vjudge_write(session))
    if report["accepted"] and report["rejected"]:
        report["message"] = "部分行被拒绝，已接受的提交已处理"
    print(json.dumps(report, ensure_ascii=False))
    return int(report["rejected"] > 0 and report["accepted"] == 0)


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
            elif args.command == "oj-sync":
                result = run_oj_job(session, args.limit)
                print(json.dumps(result, ensure_ascii=False))
                if result.get("error"):
                    return 1
            elif args.command == "vjudge-import":
                return import_vjudge_file(session, args.file)
            elif args.command == "runs":
                rows = runs(session, args.limit)
                for row in rows:
                    print(
                        f"{row.id}\t{row.job_name}\t{row.biz_date}\t{row.term}\t{row.status}"
                        f"\tattempts={row.attempts}\t{row.started_at}\t"
                        f"{(row.finished_at or '')}\t{(row.detail or '')}"
                    )
    except JobRunning as exc:
        print(f"40902 {exc}", file=sys.stderr)
        return 1
    except OjError as exc:
        print(f"{exc.code} {exc}", file=sys.stderr)
        return 1
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
