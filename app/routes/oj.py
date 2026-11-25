# OJ 账号、提交记录、导入、同步与任务状态。
import json

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import (
    JSONResponse,
)
from python_multipart.exceptions import MultipartParseError
from sqlalchemy import select, true
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from bin.job import OJ_JOB, JobFailed, JobRunning, oj_job_date, reserve_job, run_oj_job

from .. import routes
from ..auth import (
    audit,
    authenticated,
    current_member,
    deny,
    permission_codes,
    validate_csrf,
)
from ..db import SessionLocal, get_session
from ..models import (
    JobRun,
    Member,
    OjAccount,
    OjSubmission,
    Problem,
)
from ..oj import (
    MAX_LIMIT,
    PLATFORMS,
    VERDICTS,
    OjError,
    bind_account,
    import_vjudge_csv,
    platform_name,
    sync_limit,
    sync_platform_name,
    unbind_account,
)
from ..storage import CHUNK_BYTES
from ..web import content_type, page, success, wants_html
from ._common import (
    api_error,
    bank_write,
    int_field,
    object_id,
    parse_day_field,
    payload,
    redirect,
    required_text,
    s5_page,
    s5_pagination,
)

router = APIRouter()


OJ_GLOBAL_ROLES = {"coach", "sys_admin"}


def check_oj_global(request, session, actor, target_type="oj"):
    if actor.role not in OJ_GLOBAL_ROLES:
        deny(request, session, target_type=target_type)
    return actor


def oj_approver(request: Request, actor: Member = Depends(authenticated),
                session: Session = Depends(get_session)):
    return check_oj_global(request, session, actor)


def job_reader(request: Request, id: int = Depends(object_id),
               actor: Member = Depends(authenticated), session: Session = Depends(get_session)):
    if actor.role not in OJ_GLOBAL_ROLES:
        deny(request, session, id, "job_run")
    return actor


def oj_target(request, session, actor, target_id, *, write=False):
    if target_id is None or target_id == actor.id:
        return actor
    if write or (actor.role not in OJ_GLOBAL_ROLES and (
        actor.role != "dept_admin" or actor.dept_id is None
    )):
        deny(request, session, target_id, "oj_account")
    statement = select(Member).where(Member.id == target_id, Member.deleted_at.is_(None))
    if actor.role == "dept_admin":
        target = session.scalar(statement.where(Member.dept_id == actor.dept_id))
        if target is None:
            deny(request, session, target_id, "oj_account")
    else:
        target = session.scalar(statement)
        if target is None:
            raise api_error(404, 40401, "会员不存在")
    return target


def oj_account_scope(actor):
    if actor.role in OJ_GLOBAL_ROLES:
        return true()
    if actor.role == "dept_admin" and actor.dept_id is not None:
        return OjAccount.member_id.in_(select(Member.id).where(
            Member.dept_id == actor.dept_id, Member.deleted_at.is_(None),
        ))
    return OjAccount.member_id == actor.id


def account_item(account):
    return {field: getattr(account, field) for field in (
        "id", "member_id", "platform", "handle", "created_at", "verified_at",
    )}


def oj_accounts_page(request, session, actor, target, *, failure=None):
    accounts = session.scalars(select(OjAccount).where(
        OjAccount.member_id == target.id, oj_account_scope(actor),
    ).order_by(OjAccount.platform)).all()
    items = [account_item(account) for account in accounts]
    if wants_html(request):
        return page(
            request, "oj_accounts.html", accounts=items, target=target, platforms=PLATFORMS,
            status=failure.status if failure else 200,
            error_code=failure.code if failure else None,
            error_message=str(failure) if failure else None,
        )
    return success({"items": items, "member_id": target.id})


@router.get("/oj/accounts")
def list_oj_accounts(request: Request, member_id: int | None = Query(None, ge=1, le=2**63-1),
                     actor: Member = Depends(authenticated), session: Session = Depends(get_session)):
    return oj_accounts_page(request, session, actor, oj_target(request, session, actor, member_id))


@router.post("/oj/accounts")
@router.post("/oj/accounts/unbind")
def write_oj_account(request: Request, actor: Member = Depends(authenticated), data=Depends(payload),
                     session: Session = Depends(get_session)):
    unbind = request.url.path.endswith("/unbind")
    try:
        for source in (data, request.query_params):
            if "member_id" in source:
                target_id = int_field(source, "member_id", required=True)
                if unbind and target_id != actor.id:
                    raise OjError("要解绑的账号不存在", code=40401, status=404)
                oj_target(request, session, actor, target_id, write=True)
        with bank_write(session, "该账号已绑定", legacy_busy_response=False):
            platform = platform_name(data.get("platform"))
            if unbind:
                account = unbind_account(
                    session, actor.id, platform,
                    account_id=int_field(data, "account_id", required=True) if "account_id" in data else None,
                    handle=required_text(data, "handle", "账号", 80) if "handle" in data else None,
                )
            else:
                account = bind_account(session, actor.id, platform, data.get("handle"))
            result = account_item(account)
            audit(request, session, "oj.account.unbound" if unbind else "oj.account.bound",
                  actor_id=actor.id, target_id=account.id, target_type="oj_account", detail=result)
    except OjError as exc:
        session.rollback()
        if wants_html(request):
            return oj_accounts_page(request, session, actor, actor, failure=exc)
        raise api_error(exc.status, exc.code, str(exc)) from None
    if wants_html(request):
        return redirect("/oj/accounts")
    return success({"unbound": True} if unbind else result)


@router.get("/oj/submissions")
def list_oj_submissions(request: Request, actor: Member = Depends(authenticated),
                        pagination=Depends(s5_pagination), session: Session = Depends(get_session),
                        member_id: int | None = Query(None, ge=1, le=2**63-1),
                        platform: str | None = Query(None), verdict: str | None = Query(None, max_length=30),
                        date_from: str | None = Query(None), date_to: str | None = Query(None)):
    statement = (
        select(OjSubmission, OjAccount, Problem.title)
        .outerjoin(OjAccount, OjAccount.id == OjSubmission.oj_account_id)
        .outerjoin(Problem, Problem.id == OjSubmission.problem_id)
    )
    if member_id is not None:
        oj_target(request, session, actor, member_id)
        statement = statement.where(OjAccount.member_id == member_id)
    statement = statement.where(oj_account_scope(actor))

    if platform:
        try:
            platform = platform_name(platform)
        except OjError as exc:
            raise api_error(exc.status, exc.code, str(exc)) from None
        statement = statement.where(OjSubmission.platform == platform)
    if verdict:
        verdict = verdict.upper()
        statement = statement.where(OjSubmission.verdict == verdict)
    dates = {"date_from": date_from, "date_to": date_to}
    start = parse_day_field(dates, "date_from", "起始日期", required=False)
    end = parse_day_field(dates, "date_to", "结束日期", required=False)
    if start and end and start > end:
        raise api_error(400, 40001, "起始日期须早于或等于结束日期")
    if start:
        statement = statement.where(OjSubmission.submitted_at >= start.isoformat())
    if end:
        statement = statement.where(OjSubmission.submitted_at <= f"{end.isoformat()} 23:59:59.999999")
    rows, metadata = s5_page(session, statement.order_by(
        OjSubmission.submitted_at.desc(), OjSubmission.id.desc(),
    ), pagination)
    items = [{
        "id": submission.id, "platform": submission.platform, "external_id": submission.external_id,
        "handle": account.handle if account else None, "member_id": account.member_id if account else None,
        "problem_id": submission.problem_id, "problem_title": title,
        "verdict": submission.verdict, "submitted_at": submission.submitted_at,
        "synced_at": submission.synced_at,
    } for submission, account, title in rows]
    data = {**metadata, "items": items}
    if wants_html(request):
        return page(request, "oj_submissions.html", submissions=data, platforms=PLATFORMS,
                    verdicts=VERDICTS, filters={**dates, "platform": platform, "verdict": verdict,
                                               "member_id": member_id})
    return success(data)


def csv_import_page(request, *, report=None, failure=None):
    return page(request, "oj_import.html", report=report, status=failure.status if failure else 200,
                error_code=failure.code if failure else None,
                error_message=str(failure) if failure else None)


@router.get("/oj/vjudge-import")
def vjudge_import_form(request: Request, actor: Member = Depends(oj_approver)):
    return csv_import_page(request)


async def vjudge_csv_upload(request: Request, actor: Member = Depends(current_member),
                            session: Session = Depends(get_session)):
    check_oj_global(request, session, actor)
    if wants_html(request):
        request.state.navigation_codes = permission_codes(actor, session)
    length = request.headers.get("content-length")
    if length is not None:
        try:
            within_limit = 0 <= int(length) <= routes.MAX_CSV_BYTES
        except ValueError:
            within_limit = False
        if not within_limit:
            raise api_error(400, 40001, "上传请求不能超过 5 MiB")
    if content_type(request) != "multipart/form-data":
        raise api_error(400, 40001, "请选择 UTF-8 CSV 文件")

    async def bounded_body():
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > routes.MAX_CSV_BYTES:
                raise MultiPartException("上传请求不能超过 5 MiB")
            for offset in range(0, len(chunk), CHUNK_BYTES):
                yield chunk[offset:offset + CHUNK_BYTES]

    try:
        form = await MultiPartParser(
            request.headers, bounded_body(), max_files=1, max_fields=1, max_part_size=1024,
        ).parse()
    except MultiPartException as exc:
        raise api_error(400, 40001, str(exc)) from None
    except MultipartParseError:
        raise api_error(400, 40001, "上传内容无效") from None
    try:
        await validate_csrf(request, session, form)
        yield actor, form.get("file")
    finally:
        await form.close()


@router.post("/oj/vjudge-import")
def upload_vjudge_csv(request: Request, upload=Depends(vjudge_csv_upload),
                      session: Session = Depends(get_session)):
    actor, file = upload
    try:
        if not isinstance(file, UploadFile):
            raise OjError("请选择 UTF-8 CSV 文件")
        raw = file.file.read(routes.MAX_CSV_BYTES + 1)

        def record_import(report):
            audit(request, session, "oj.vjudge.imported", actor_id=actor.id, target_type="oj_submission",
                  detail={key: report[key] for key in ("accepted", "rejected", "created", "skipped")})

        report = import_vjudge_csv(
            session, raw, write=bank_write(session, "导入冲突，请重试", legacy_busy_response=False),
            record=record_import,
        )
    except OjError as exc:
        if wants_html(request):
            return csv_import_page(request, failure=exc)
        raise api_error(exc.status, exc.code, str(exc)) from None
    if wants_html(request):
        return csv_import_page(request, report=report)
    return success(report)


def oj_sync_page(request, session, *, failure=None):
    latest_id = session.scalar(select(JobRun.id).where(JobRun.job_name == OJ_JOB).order_by(
        JobRun.biz_date.desc(),
    ).limit(1))
    return page(
        request, "oj_sync.html", limit=sync_limit(session), max_limit=MAX_LIMIT, latest_id=latest_id,
        status=failure.status if failure else 200, error_code=failure.code if failure else None,
        error_message=str(failure) if failure else None,
    )


@router.get("/oj/sync")
def oj_sync_form(request: Request, actor: Member = Depends(oj_approver),
                 session: Session = Depends(get_session)):
    return oj_sync_page(request, session)


def complete_oj_sync(engine, reservation, limit, request_id, actor_id):
    with SessionLocal(bind=engine) as worker_session:
        try:
            run_oj_job(worker_session, limit, reservation=reservation, request_id=request_id, actor_id=actor_id)
        except JobFailed:
            # run_job persists and logs the failure before the polling endpoint exposes it.
            return


@router.post("/oj/sync")
@router.post("/oj/sync-submissions")
def start_oj_sync(request: Request, background: BackgroundTasks,
                  actor: Member = Depends(oj_approver), data=Depends(payload),
                  session: Session = Depends(get_session)):
    try:
        sync_platform_name(data.get("platform", "codeforces"))
        limit = sync_limit(session, data.get("limit"))
        reservation = reserve_job(session, OJ_JOB, oj_job_date(), exclusive=True)
        run_id = reservation.id
        audit(request, session, "oj.sync.requested", actor_id=actor.id, target_type="job_run",
              target_id=run_id, detail={"platform": "codeforces", "limit": limit})
        session.commit()
    except (OjError, JobRunning) as exc:
        session.rollback()
        failure = OjError(str(exc), status=409, code=40902) if isinstance(exc, JobRunning) else exc
        if wants_html(request):
            return oj_sync_page(request, session, failure=failure)
        raise api_error(failure.status, failure.code, str(failure)) from None

    background.add_task(complete_oj_sync, request.app.state.engine, reservation, limit,
                        request.state.request_id, actor.id)
    if wants_html(request):
        return redirect(f"/jobs/{run_id}")
    return JSONResponse(success({"jobRunId": run_id}), status_code=202,
                        headers={"Location": f"/jobs/{run_id}"})


@router.get("/jobs/{id}")
@router.get("/oj/sync-tasks/{id}")
def job_status(request: Request, id: int = Depends(object_id), actor: Member = Depends(job_reader),
               session: Session = Depends(get_session)):
    row = session.get(JobRun, id)
    if row is None or (request.url.path.startswith("/oj/") and row.job_name != OJ_JOB):
        raise api_error(404, 40401, "作业不存在")
    try:
        detail = json.loads(row.detail) if row.detail else None
    except ValueError:
        detail = {"message": row.detail}
    data = {field: getattr(row, field) for field in (
        "id", "job_name", "biz_date", "term", "status", "attempts", "started_at", "finished_at",
    )}
    data["detail"] = detail
    if wants_html(request):
        return page(request, "job_status.html", job=data)
    return success(data)
