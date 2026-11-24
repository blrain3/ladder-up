from sqlalchemy import inspect


def test_upgrade_from_empty_database(engine, migration_head):
    tables = set(inspect(engine).get_table_names())
    assert {
        "department", "member", "permission_point", "member_permission", "member_session",
        "member_application", "attachment", "audit_log", "sys_config", "knowledge_point",
        "problem", "problem_kp", "problem_solution", "alembic_version",
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
