import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import make_engine


@pytest.fixture
def database_url(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'app.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("SESSION_TTL_HOURS", "8")
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    return url


@pytest.fixture
def engine(database_url):
    engine = make_engine(database_url)
    try:
        yield engine
    finally:
        engine.dispose()
