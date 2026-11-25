import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryFile
from time import perf_counter
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from .auth import active_member, allows
from .config import Settings
from .db import SessionLocal, database_path, get_session, make_engine
from .models import Member
from .routes import router
from .security import load_session
from .web import APP_DIR, page, templates, wants_html

templates.env.globals["allows"] = allows


def error(status, code, message, headers=None, data=None):
    return JSONResponse(
        status_code=status,
        content={"code": code, "message": message, "data": data},
        headers=headers,
    )


def database_unavailable():
    return error(503, 50301, "数据库不可写，请重试", {"Retry-After": "1"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    app.state.settings = settings
    app.state.database_file = database_path(settings.database_url)
    app.state.uploads_dir = Path(settings.uploads_dir)
    engine = make_engine(settings.database_url)
    app.state.engine = engine

    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    handler = logging.FileHandler(log_dir / "app.log", encoding="utf-8")
    logger = logging.Logger("ladder_up.requests")
    logger.addHandler(handler)
    app.state.request_logger = logger
    try:
        yield
    finally:
        handler.close()
        engine.dispose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
app.include_router(router)


def requires_password_change(request, token):
    with SessionLocal(bind=request.app.state.engine) as session:
        current = load_session(token, session=session)
        member = session.get(Member, current.member_id) if current else None
        if member is not None and active_member(member) and member.must_change_password:
            request.state.member_id = member.id
            return True
        return False


@app.middleware("http")
async def log_request(request: Request, call_next):
    started = perf_counter()
    request_id = str(uuid4())
    request.state.request_id = request_id
    status = 500
    try:
        path = request.url.path
        token = request.cookies.get("sid")
        if (token and request.method in {"GET", "POST", "PUT", "PATCH", "DELETE"}
                and path not in {"/me/password", "/auth/logout"}
                and await run_in_threadpool(requires_password_change, request, token)):
            response = RedirectResponse("/me/password", status_code=303, headers={"Cache-Control": "no-store"})
        else:
            response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        if not request.url.path.startswith(("/static/", "/files/")):
            response.headers["Cache-Control"] = "no-store"
        return response
    finally:
        request.app.state.request_logger.info(json.dumps({
            "request_id": request_id,
            "member_id": getattr(request.state, "member_id", None),
            "path": request.url.path,
            "status": status,
            "ms": round((perf_counter() - started) * 1000, 2),
        }, ensure_ascii=False))


def request_error(request, status, code, message, headers=None, data=None):
    if wants_html(request) and request.url.path != "/health":
        return page(
            request, "error.html", status=status, headers=headers,
            error_code=code, error_message=message,
        )
    return error(status, code, message, headers, data)


@app.exception_handler(HTTPException)
async def handle_http_error(request, exc):
    code = {502: 50002}.get(exc.status_code, exc.status_code * 100 + 1)
    data = None
    if isinstance(exc.detail, dict):
        code, message = exc.detail["code"], exc.detail["message"]
        data = exc.detail.get("data")
    else:
        message = exc.detail if isinstance(exc.detail, str) else "请求失败"
    return request_error(request, exc.status_code, code, message, exc.headers, data)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request, _exc):
    return request_error(request, 400, 40001, "参数校验失败")


@app.exception_handler(OperationalError)
async def handle_database_error(request, exc):
    sqlite_code = getattr(exc.orig, "sqlite_errorcode", 0)
    if sqlite_code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_READONLY}:
        return request_error(request, 503, 50301, "数据库不可写，请重试", {"Retry-After": "1"})
    return await handle_internal_error(request, exc)


@app.exception_handler(Exception)
async def handle_internal_error(request, exc):
    logging.getLogger(__name__).error(
        "Unhandled %s in request %s", type(exc).__name__, request.state.request_id
    )
    return request_error(request, 500, 50001, "服务内部错误")


@app.get("/health")
def health(request: Request, session: Session = Depends(get_session)):
    database_file = request.app.state.database_file
    try:
        if not database_file.is_file():
            return database_unavailable()
        with TemporaryFile(dir=database_file.parent):
            session.execute(text("SELECT 1")).scalar_one()
            migration = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            # Acquire a write transaction without changing any persisted rows.
            session.execute(text("BEGIN IMMEDIATE"))
            session.rollback()
    except (OSError, SQLAlchemyError, RuntimeError):
        return database_unavailable()
    return {"code": 0, "message": "ok", "data": {"db": "ok", "migration": migration}}
