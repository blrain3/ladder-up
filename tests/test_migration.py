from sqlalchemy import inspect


def test_upgrade_from_empty_database(engine, migration_head):
    tables = set(inspect(engine).get_table_names())
    assert {
        "department", "member", "permission_point", "member_permission", "member_session",
        "member_application", "attachment", "audit_log", "sys_config", "knowledge_point",
        "problem", "problem_kp", "problem_solution", "training_plan", "training_phase",
        "training_task", "task_problem", "task_assignee", "checkin", "checkin_problem",
        "assessment_test", "test_score", "member_assessment", "job_run", "alembic_version",
    } <= tables
    with engine.connect() as connection:
        revision = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
        assert revision == migration_head


def test_s2_repairs_legacy_columns_and_downgrades_only_its_changes(tmp_path, monkeypatch):
    from alembic.config import Config

    from alembic import command
    from app.db import make_engine

    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv("COOKIE_SECURE", raising=False)
    config = Config("alembic.ini")
    command.upgrade(config, "0002")
    engine = make_engine(url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP INDEX uk_app_active")
            connection.exec_driver_sql("ALTER TABLE member_application DROP COLUMN interviewed_at")
            connection.exec_driver_sql("""
                INSERT INTO member_application (student_no, name, apply_type, status, created_at)
                VALUES ('202400000001', '旧申请', '入会', '待面试', '2025-01-01')
            """)
        command.upgrade(config, "head")
        assert {"uk_app_active", "uk_app_receipt_code_hash"} <= {
            index["name"] for index in inspect(engine).get_indexes("member_application")
        }
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT active_flag FROM member_application").scalar_one() == "1"
        command.downgrade(config, "0002")
        inspector = inspect(engine)
        application_columns = {c["name"] for c in inspector.get_columns("member_application")}
        assert "active_flag" in application_columns
        assert {"interviewed_at", "receipt_code_hash"}.isdisjoint(application_columns)
        assert "must_change_password" not in {c["name"] for c in inspector.get_columns("member")}
        assert {"biz_id", "deleted_at"} <= {c["name"] for c in inspector.get_columns("attachment")}
        assert "uk_app_active" not in {i["name"] for i in inspector.get_indexes("member_application")}
        command.upgrade(config, "head")
    finally:
        engine.dispose()


def test_s4_downgrade_removes_only_its_threshold(tmp_path, monkeypatch):
    from alembic.config import Config

    from alembic import command
    from app.db import make_engine

    url = f"sqlite:///{tmp_path / 's4-downgrade.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("APP_ENV", "test")
    config = Config("alembic.ini")
    engine = make_engine(url)
    query = "SELECT config_value FROM sys_config WHERE config_key = 'training.alert_threshold'"
    try:
        command.upgrade(config, "0005")
        with engine.connect() as connection:
            assert connection.exec_driver_sql(query).scalar_one() == "0.6"
        command.downgrade(config, "0004")
        assert "training_task" not in inspect(engine).get_table_names()
        with engine.begin() as connection:
            assert connection.exec_driver_sql(query).scalar_one_or_none() is None
            connection.exec_driver_sql(
                "INSERT INTO sys_config (config_key, config_value, updated_at) "
                "VALUES ('training.alert_threshold', '0.72', '2026-01-01')"
            )
        command.upgrade(config, "0005")
        command.downgrade(config, "0004")
        with engine.connect() as connection:
            assert connection.exec_driver_sql(query).scalar_one() == "0.72"
    finally:
        engine.dispose()


def test_s5_downgrade_removes_only_its_threshold(tmp_path, monkeypatch):
    from alembic.config import Config

    from alembic import command
    from app.db import make_engine

    url = f"sqlite:///{tmp_path / 's5-downgrade.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("APP_ENV", "test")
    config = Config("alembic.ini")
    engine = make_engine(url)
    query = "SELECT config_value FROM sys_config WHERE config_key = 'training.promote_threshold'"
    try:
        command.upgrade(config, "0006")
        with engine.connect() as connection:
            assert connection.exec_driver_sql(query).scalar_one() == "0.6"
        command.downgrade(config, "0005")
        assert {"assessment_test", "test_score", "member_assessment", "job_run"}.isdisjoint(
            inspect(engine).get_table_names()
        )
        assert "left_on" in {column["name"] for column in inspect(engine).get_columns("member")}
        with engine.begin() as connection:
            assert connection.exec_driver_sql(query).scalar_one_or_none() is None
            connection.exec_driver_sql(
                "INSERT INTO sys_config (config_key, config_value, updated_at) "
                "VALUES ('training.promote_threshold', '0.72', '2026-01-01')"
            )
        command.upgrade(config, "0006")
        command.downgrade(config, "0005")
        with engine.connect() as connection:
            assert connection.exec_driver_sql(query).scalar_one() == "0.72"
    finally:
        engine.dispose()
