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
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from .config import Settings
from .db import database_path, get_session, make_engine
from .routes import router
from .web import APP_DIR, page, wants_html


def error(status, code, message, headers=None):
    return JSONResponse(
        status_code=status,
        content={"code": code, "message": message, "data": None},
        headers=headers,
    )


def database_unavailable():
    return error(503, 50301, "数据库不可写，请重试", {"Retry-After": "1"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    app.state.settings = settings
    app.state.database_file = database_path(settings.database_url)
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


@app.middleware("http")
async def log_request(request: Request, call_next):
    started = perf_counter()
    request_id = str(uuid4())
    request.state.request_id = request_id
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        if not request.url.path.startswith("/static/"):
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


def request_error(request, status, code, message, headers=None):
    if wants_html(request) and request.url.path != "/health":
        return page(
            request, "error.html", status=status, headers=headers,
            error_code=code, error_message=message,
        )
    return error(status, code, message, headers)


@app.exception_handler(HTTPException)
async def handle_http_error(request, exc):
    code = {502: 50002}.get(exc.status_code, exc.status_code * 100 + 1)
    if isinstance(exc.detail, dict):
        code, message = exc.detail["code"], exc.detail["message"]
    else:
        message = exc.detail if isinstance(exc.detail, str) else "请求失败"
    return request_error(request, exc.status_code, code, message, exc.headers)


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
