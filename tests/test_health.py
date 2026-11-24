import sys

import pytest
from fastapi.testclient import TestClient

from app.main import app


BACKEND_OPTIONS = {"use_uvloop": sys.platform != "win32"}


def test_health(database_url):
    with TestClient(app, backend_options=BACKEND_OPTIONS) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"code": 0, "message": "ok", "data": {"db": "ok", "migration": "0002"}}


def test_health_rejects_unmigrated_database(database_url, engine):
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE alembic_version")
    with TestClient(app, backend_options=BACKEND_OPTIONS) as client:
        response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["code"] == 50301


def test_missing_settings_prevent_startup(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        with TestClient(app, backend_options=BACKEND_OPTIONS):
            pytest.fail("Application started without DATABASE_URL")
