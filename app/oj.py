"""OJ bindings, offline replay and submission import."""

import asyncio
import csv
import hashlib
import json
import os
import re
from contextlib import aclosing, closing
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.sqlite import insert

from .db import SessionLocal, make_engine
from .models import Member, OjAccount, OjSubmission, Problem, SysConfig
from .models.base import local_time

SHANGHAI = ZoneInfo("Asia/Shanghai")
PLATFORMS = ("codeforces", "vjudge")
CF_HANDLE = re.compile(r"[A-Za-z0-9_-]{1,80}")
VJUDGE_HANDLE = re.compile(r"[A-Za-z0-9_.-]{1,80}")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
FETCH_SECONDS = 15
MAX_CSV_BYTES = 5 * 1024 * 1024
MAX_CSV_LINES = 5000
VERDICTS = ("AC", "WA", "TLE", "MLE", "RE", "CE", "PE", "OLE", "RTE", "IE", "SE")
CF_VERDICTS = {
    "OK": "AC", "WRONG_ANSWER": "WA", "TIME_LIMIT_EXCEEDED": "TLE",
    "MEMORY_LIMIT_EXCEEDED": "MLE", "RUNTIME_ERROR": "RE", "COMPILATION_ERROR": "CE",
    "PRESENTATION_ERROR": "PE", "OUTPUT_LIMIT_EXCEEDED": "OLE",
}
CF_VALID_VERDICTS = CF_VERDICTS.keys() | {
    "FAILED", "PARTIAL", "IDLENESS_LIMIT_EXCEEDED", "SECURITY_VIOLATED", "CRASHED",
    "INPUT_PREPARATION_CRASHED", "CHALLENGED", "SKIPPED", "TESTING", "REJECTED",
}
CSV_HEADER = ["handle", "problem", "verdict", "submitted_at"]
DEFAULT_LIMIT = 100
MAX_LIMIT = 10_000


class OjError(Exception):
    def __init__(self, message, *, code=40001, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


class FetchError(Exception):
    """Contains a safe error code for job history."""


def platform_name(value):
    if not isinstance(value, str) or value.strip().lower() not in PLATFORMS:
        raise OjError("请选择有效的 OJ 平台")
    return value.strip().lower()


def sync_platform_name(value):
    if platform_name(value) != "codeforces":
        raise OjError("Vjudge 提交请通过 CSV 导入", code=40002)
    return "codeforces"


def bind_account(session, member_id, platform, handle) -> OjAccount:
    platform = platform_name(platform)
    pattern = CF_HANDLE if platform == "codeforces" else VJUDGE_HANDLE
    if not isinstance(handle, str) or not pattern.fullmatch(handle.strip()):
        extra = "、点" if platform == "vjudge" else ""
        raise OjError(f"账号须为 1–80 个英文字母、数字、连字符或下划线{extra}")
    account = session.scalar(insert(OjAccount).values(
        member_id=member_id, platform=platform, handle=handle.strip(), created_at=local_time(),
    ).on_conflict_do_nothing().returning(OjAccount))
    if account is None:
        raise OjError("该账号已绑定，或你已绑定此平台的账号", code=40901, status=409)
    return account


def unbind_account(session, member_id, platform, *, account_id=None, handle=None):
    statement = delete(OjAccount).where(
        OjAccount.member_id == member_id, OjAccount.platform == platform_name(platform),
    )
    if account_id is not None:
        statement = statement.where(OjAccount.id == account_id)
    if handle is not None:
        statement = statement.where(OjAccount.handle == handle)
    account = session.scalar(statement.returning(OjAccount))
    if account is None:
        raise OjError("要解绑的账号不存在", code=40401, status=404)
    return account


def sync_enabled(session):
    config = session.get(SysConfig, "oj.sync.enabled")
    return config is not None and config.config_value == "true"


def validated_limit(value):
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 5:
        value = int(value)
    if type(value) is not int or not 1 <= value <= MAX_LIMIT:
        raise OjError(f"拉取条数须为 1–{MAX_LIMIT} 的整数")
    return value


def sync_limit(session, override=None):
    if override is not None:
        return validated_limit(override)
    config = session.get(SysConfig, "oj.sync.limit")
    try:
        return validated_limit(config.config_value) if config else DEFAULT_LIMIT
    except OjError:
        return DEFAULT_LIMIT


def reject_json_constant(_value):
    raise ValueError("Invalid JSON constant")


async def read_cf_response(url, deadline):
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise FetchError("FETCH_TIMEOUT")
    async with asyncio.timeout(remaining), httpx.AsyncClient(
        timeout=min(10, remaining), headers={"Accept-Encoding": "identity"},
    ) as client, client.stream("GET", url) as response:
        response.raise_for_status()
        body = bytearray()
        async with aclosing(response.aiter_raw()) as chunks:
            async for chunk in chunks:
                if monotonic() >= deadline:
                    raise FetchError("FETCH_TIMEOUT")
                if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise FetchError("RESPONSE_TOO_LARGE")
                body.extend(chunk)
    payload = json.loads(body, parse_constant=reject_json_constant)
    status_rows(payload)
    if monotonic() >= deadline:
        raise FetchError("FETCH_TIMEOUT")
    return payload


@dataclass
class CodeforcesRequests:
    enabled: bool
    limit: int
    next_request_at: float | None = None

    def fetch(self, handle):
        url = "https://codeforces.com/api/user.status?" + urlencode({
            "handle": handle, "from": 1, "count": self.limit,
        })
        deadline = monotonic() + FETCH_SECONDS
        for attempt, backoff in enumerate((0, 1, 2, 4)):
            now = monotonic()
            pace = self.next_request_at - now if self.next_request_at is not None else 0
            delay = max(backoff, pace)
            if now + max(0, delay) >= deadline:
                raise FetchError("FETCH_TIMEOUT")
            if delay > 0:
                sleep(delay)
            self.next_request_at = monotonic() + 2
            try:
                # Closing the loop does not wait for an already-cancelled DNS lookup.
                with closing(asyncio.new_event_loop()) as loop:
                    try:
                        return loop.run_until_complete(read_cf_response(url, deadline))
                    finally:
                        loop.run_until_complete(loop.shutdown_asyncgens())
            except httpx.HTTPStatusError as exc:
                error = str(exc.response.status_code)
            except TimeoutError:
                raise FetchError("FETCH_TIMEOUT") from None
            except httpx.TimeoutException:
                error = "FETCH_TIMEOUT"
                if monotonic() >= deadline:
                    raise FetchError(error) from None
            except httpx.RequestError:
                error = "NETWORK_ERROR"
            except (ValueError, RecursionError):
                error = "INVALID_RESPONSE"
            except FetchError as exc:
                error = str(exc)
                if error == "FETCH_TIMEOUT":
                    raise
            if attempt == 3:
                raise FetchError(error)


_current_requests: ContextVar[CodeforcesRequests | None] = ContextVar("oj_requests", default=None)


def default_requests(limit):
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return CodeforcesRequests(False, limit)
    engine = make_engine(database_url)
    try:
        with SessionLocal(bind=engine) as session:
            return CodeforcesRequests(sync_enabled(session), limit)
    finally:
        engine.dispose()


def fetch_user_status(handle, platform):
    """Replay user.status from disk when configured, otherwise honor the DB switch."""
    sync_platform_name(platform)
    fixture = os.getenv("OJ_FIXTURE_PATH")
    if fixture:
        try:
            with Path(fixture).open(encoding="utf-8") as stream:
                return json.load(stream, parse_constant=reject_json_constant)
        except (OSError, ValueError, RecursionError) as exc:
            raise FetchError("INVALID_FIXTURE") from exc
    requester = _current_requests.get() or default_requests(DEFAULT_LIMIT)
    if not requester.enabled:
        return {"skip": "disabled"}
    return requester.fetch(handle)


def status_rows(payload):
    if isinstance(payload, dict) and payload.get("status") == "FAILED":
        comment = str(payload.get("comment", "")).lower()
        raise FetchError("429" if "limit exceeded" in comment else "CF_FAILED")
    rows = payload.get("result") if isinstance(payload, dict) and payload.get("status") == "OK" else None
    if not isinstance(rows, list):
        raise FetchError("INVALID_RESPONSE")
    return rows


def normalize_submission(row, handle):
    try:
        members = row["author"]["members"]
        if not isinstance(members, list) or not members:
            raise ValueError
        handles = [member["handle"] for member in members]
        if not all(isinstance(name, str) and CF_HANDLE.fullmatch(name) for name in handles):
            raise ValueError
        problem = row["problem"]
        contest = problem.get("contestId", row.get("contestId"))
        index = problem["index"]
        timestamp = row["creationTimeSeconds"]
        external_id = row["id"]
        verdict = row["verdict"]
        if type(contest) is not int or contest <= 0 or not isinstance(index, str) or not re.fullmatch(
            r"[A-Za-z0-9]{1,20}", index,
        ):
            raise ValueError
        if type(timestamp) is not int or timestamp <= 0 or type(external_id) is not int or external_id <= 0:
            raise ValueError
        if not isinstance(verdict, str) or verdict not in CF_VALID_VERDICTS:
            raise ValueError
        submitted = datetime.fromtimestamp(timestamp, SHANGHAI)
        if handle.lower() not in {name.lower() for name in handles}:
            return None
        return {
            "external_id": str(external_id),
            "problem": f"{contest}{index}",
            "verdict": CF_VERDICTS.get(verdict, verdict),
            "submitted_at": submitted.strftime("%Y-%m-%d %H:%M:%S"),
        }
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError) as exc:
        raise FetchError("INVALID_SUBMISSION") from exc


def fetch_submissions(handle, platform, limit) -> list[dict]:
    limit = validated_limit(limit)
    token = None
    if _current_requests.get() is None and not os.getenv("OJ_FIXTURE_PATH"):
        token = _current_requests.set(default_requests(limit))
    try:
        payload = fetch_user_status(handle, platform)
        if isinstance(payload, dict) and payload.get("skip") == "disabled":
            return []
        rows = []
        accepted = 0
        for raw in status_rows(payload):
            try:
                row = normalize_submission(raw, handle)
            except FetchError as exc:
                rows.append({"error": str(exc)})
                continue
            if row is not None:
                rows.append(row)
                accepted += 1
                if accepted == limit:
                    break
        return rows
    finally:
        if token is not None:
            _current_requests.reset(token)


def problem_ids(session, platform, external_ids):
    external_ids = list(set(external_ids))
    statement = select(Problem.external_id, Problem.id).where(
        Problem.platform == platform, Problem.deleted_at.is_(None),
    )
    problems = {}
    for offset in range(0, len(external_ids), 500):
        problems.update(session.execute(statement.where(
            Problem.external_id.in_(external_ids[offset:offset + 500]),
        )).all())
    return problems


def store_submissions(session, platform, rows):
    if not rows:
        return 0
    synced_at = local_time()
    statement = insert(OjSubmission).on_conflict_do_nothing(
        index_elements=["platform", "external_id"],
    )
    result = session.connection().execute(statement, [
        {**row, "platform": platform, "synced_at": synced_at} for row in rows
    ])
    return result.rowcount


def csv_row(line):
    try:
        fields = next(csv.reader([line], strict=True))
    except csv.Error as exc:
        raise OjError("CSV 格式错误") from exc
    if len(fields) != len(CSV_HEADER):
        raise OjError("应有四列")
    return [field.strip() for field in fields]


def parse_vjudge_csv(raw):
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > MAX_CSV_BYTES:
        raise OjError("CSV 文件不能超过 5 MiB")
    try:
        csv_text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise OjError("CSV 文件须使用 UTF-8 编码") from None
    stream = StringIO(csv_text, newline="")
    try:
        if csv_row(stream.readline()) != CSV_HEADER:
            raise OjError("表头不匹配")
    except OjError:
        raise OjError("表头须为 handle,problem,verdict,submitted_at") from None
    parsed = []
    # Each submission occupies one physical line so a malformed quote cannot swallow later rows.
    for line_number, line in enumerate(stream, 2):
        if line_number > MAX_CSV_LINES:
            raise OjError(f"CSV 不能超过 {MAX_CSV_LINES} 个物理行")
        try:
            handle, problem, verdict, submitted_at = csv_row(line)
            if not problem or len(problem) > 100:
                raise OjError("题号须为 1–100 个字符")
            if verdict not in VERDICTS:
                raise OjError("verdict 不在允许集合")
            try:
                submitted = datetime.fromisoformat(submitted_at)
                if submitted.tzinfo is not None:
                    submitted = submitted.astimezone(SHANGHAI)
                submitted_at = submitted.replace(tzinfo=None).isoformat(sep=" ")
            except (OverflowError, ValueError):
                raise OjError("submitted_at 无法解析") from None
        except OjError as exc:
            parsed.append((line_number, None, str(exc)))
            continue

        # The export has no submission ID; its normalized four columns identify a replay.
        key = json.dumps([handle.lower(), problem, verdict, submitted_at], ensure_ascii=False)
        row = {"external_id": hashlib.sha256(key.encode()).hexdigest(), "verdict": verdict,
               "submitted_at": submitted_at}
        parsed.append((line_number, (handle.lower(), problem, row), None))
    return parsed


def store_vjudge_rows(session, parsed):
    accounts = {
        handle.lower(): account_id
        for handle, account_id in session.execute(
            select(OjAccount.handle, OjAccount.id).where(OjAccount.platform == "vjudge")
        )
    }
    problems = problem_ids(session, "vjudge", (entry[1] for _, entry, _ in parsed if entry))
    errors = []
    rows = []
    for line, entry, reason in parsed:
        if entry is not None:
            handle, problem, row = entry
            account_id = accounts.get(handle)
            if account_id is not None:
                rows.append({**row, "oj_account_id": account_id, "problem_id": problems.get(problem)})
                continue
            reason = "handle 未绑定 Vjudge 账号"
        if len(errors) < 20:
            errors.append({"line": line, "reason": reason})
    created = store_submissions(session, "vjudge", rows)
    return {"accepted": len(rows), "rejected": len(parsed) - len(rows), "errors": errors,
            "created": created, "skipped": len(rows) - created}


def import_vjudge_csv(session, raw, *, write, record=None):
    parsed = parse_vjudge_csv(raw)
    with write:
        report = store_vjudge_rows(session, parsed)
        if record is not None:
            record(report)
        return report


def sync_platform(session, platform, limit, *, renew_lease=None) -> dict:
    """Fetch accounts serially and commit each batch after its network request."""
    platform = sync_platform_name(platform)
    result = {"created": 0, "skipped": 0, "errors": 0, "accounts_succeeded": 0, "error": None}
    enabled = bool(os.getenv("OJ_FIXTURE_PATH")) or sync_enabled(session)
    if not enabled:
        return {**result, "skip": "disabled"}
    requester = CodeforcesRequests(enabled, sync_limit(session, limit))
    accounts = session.execute(
        select(OjAccount.id, OjAccount.handle)
        .join(Member).where(
            OjAccount.platform == platform, Member.deleted_at.is_(None),
            Member.member_status.not_in(("已退会", "已除名")),
        ).order_by(OjAccount.id)
    ).all()
    session.commit()
    token = _current_requests.set(requester)
    try:
        for account_id, handle in accounts:
            try:
                rows = fetch_submissions(handle, platform, requester.limit)
            except FetchError as exc:
                rows = [{"error": str(exc)}]
            valid = [row for row in rows if "error" not in row]
            errors = [row["error"] for row in rows if "error" in row]
            if errors:
                result["errors"] += 1
                result["error"] = errors[-1]
            else:
                result["accounts_succeeded"] += 1
            if not valid and renew_lease is None:
                continue
            session.execute(text("BEGIN IMMEDIATE"))
            if renew_lease is not None:
                renew_lease(session)
            # Unbinding during a fetch must not attribute results to a replacement account.
            if not valid or session.get(OjAccount, account_id) is None:
                session.commit()
                result["skipped"] += len(valid)
                continue
            problems = problem_ids(session, platform, (row["problem"] for row in valid))
            created = store_submissions(session, platform, [
                {"external_id": row["external_id"], "verdict": row["verdict"],
                 "submitted_at": row["submitted_at"], "oj_account_id": account_id,
                 "problem_id": problems.get(row["problem"])}
                for row in valid
            ])
            session.commit()
            result["created"] += created
            result["skipped"] += len(valid) - created
        return result
    finally:
        _current_requests.reset(token)
