from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError, OperationalError

from app.models import Department, MemberApplication, MemberPermission
from app.models.base import local_time


def test_pragmas_on_each_physical_connection(engine):
    with engine.connect() as first, engine.connect() as second:
        assert first.connection.driver_connection is not second.connection.driver_connection
        for connection in (first, second):
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000


def test_orphan_foreign_key_is_rejected(engine):
    with engine.connect() as connection:
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            connection.execute(insert(MemberPermission), {
                "member_id": 1, "permission_id": 1, "granted_at": local_time(),
            })


def test_only_one_active_application_per_type(engine):
    values = {
        "student_no": "test01", "name": "示例", "apply_type": "入会",
        "status": "待审核", "active_flag": "1", "created_at": local_time(),
    }
    with engine.begin() as connection:
        connection.execute(insert(MemberApplication), values)
        with pytest.raises(IntegrityError, match="UNIQUE"):
            connection.execute(insert(MemberApplication), values)
        history = dict(values, status="已拒绝", active_flag=None)
        connection.execute(insert(MemberApplication), [history, history])
        assert connection.scalar(select(func.count()).select_from(MemberApplication)) == 3


@pytest.mark.parametrize("timeout", [5000, 100])
def test_concurrent_writers(engine, timeout):
    writer_locked = Event()
    contender_entering = Event()
    release_writer = Event()
    now = local_time()

    def row(index):
        return {"name": f"测试部门 {index}", "type": "其他", "created_at": now, "updated_at": now}

    def batch_writer():
        with engine.begin() as connection:
            connection.execute(insert(Department), [row(index) for index in range(100)])
            writer_locked.set()
            assert release_writer.wait(8)

    def single_writer():
        with engine.begin() as connection:
            if timeout == 100:
                connection.exec_driver_sql("PRAGMA busy_timeout=100")
            contender_entering.set()
            connection.execute(insert(Department), row(100))

    with ThreadPoolExecutor(max_workers=2) as executor:
        batch = executor.submit(batch_writer)
        try:
            assert writer_locked.wait(5)
            single = executor.submit(single_writer)
            assert contender_entering.wait(5)
            if timeout == 100:
                with pytest.raises(OperationalError, match="database is locked"):
                    single.result(timeout=2)
            else:
                with pytest.raises(TimeoutError):
                    single.result(timeout=0.2)
        finally:
            release_writer.set()
        batch.result(timeout=5)
        if timeout == 5000:
            single.result(timeout=5)

    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Department)) == (
            101 if timeout == 5000 else 100
        )
