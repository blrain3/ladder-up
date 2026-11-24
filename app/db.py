from collections.abc import Iterator
from pathlib import Path

from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker


SessionLocal = sessionmaker(expire_on_commit=False)


def database_path(database_url: str) -> Path:
    url = make_url(database_url)
    if (url.drivername not in {"sqlite", "sqlite+pysqlite"}
            or not url.database or url.database == ":memory:"
            or url.query.get("uri") == "true"):
        raise ValueError("DATABASE_URL must point to a SQLite file")
    return Path(url.database).resolve()


def make_engine(database_url: str) -> Engine:
    database_path(database_url)
    engine = create_engine(database_url, hide_parameters=True)

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _record):
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            if cursor.fetchone()[0].lower() != "wal":
                raise RuntimeError("SQLite could not enable WAL")
        finally:
            cursor.close()

    return engine


def get_session(request: Request) -> Iterator[Session]:
    with SessionLocal(bind=request.app.state.engine) as session:
        yield session
