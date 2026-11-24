import bin.seed as seed_module


def test_seed_uses_demo_password_for_members(database_url, client_factory, monkeypatch, capsys):
    monkeypatch.setenv("SEED_DEMO_PASSWORD", "Demo1234!")
    monkeypatch.setenv("SEED_ADMIN_PASSWORD", "ChangeMe123!")
    seed_module.seed(database_url)
    captured = capsys.readouterr()
    assert "demo0001 / member / SEED_DEMO_PASSWORD" in captured.err
    for secret in ("$2", "Demo1234!", "ChangeMe123!"):
        assert secret not in captured.out + captured.err

    client = client_factory()
    response = client.post("/auth/login", json={"student_no": "demo0001", "password": "Demo1234!"})
    assert response.status_code == 303
    assert client.get("/me").json()["data"]["role"] == "member"

    response = client.post("/auth/login", json={"student_no": "admin0001", "password": "ChangeMe123!"})
    assert response.status_code == 303
    assert client.get("/me").json()["data"]["role"] == "sys_admin"


def test_seed_requires_admin_password_outside_demo(database_url, monkeypatch):
    import pytest

    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(RuntimeError, match="SEED_ADMIN_PASSWORD"):
        seed_module.seed(database_url)
