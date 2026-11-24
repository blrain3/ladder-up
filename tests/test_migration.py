from sqlalchemy import inspect


def test_upgrade_from_empty_database(engine):
    tables = set(inspect(engine).get_table_names())
    assert {
        "department", "member", "permission_point", "member_permission", "member_session",
        "member_application", "attachment", "audit_log", "sys_config", "alembic_version",
    } <= tables
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0002"
