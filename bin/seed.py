import os
import sys
from pathlib import Path

import bcrypt
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

# Support direct execution as python bin/seed.py.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.db import SessionLocal, make_engine
from app.models import Department, Member, MemberPermission, PermissionPoint, SysConfig
from app.models.base import local_time

DEFAULT_PERMISSIONS = {
    "member": ("member:read:self", "problem:read", "training:read:self", "checkin:write:self"),
    "dept_admin": ("member:read", "member:write", "training:read", "training:write", "problem:read"),
    "coach": ("member:read", "member:approve", "problem:read", "problem:write",
              "training:read", "training:write", "training:approve", "contest:*"),
}
OTHER_PERMISSIONS = (
    "contest:read", "contest:write", "contest:approve",
    "camp:read", "camp:write", "camp:approve", "team:read", "team:approve", "system:admin",
)
MODULES = {
    "member": "A", "problem": "B", "training": "B", "checkin": "B",
    "camp": "C", "contest": "D", "team": "E", "system": "F",
}


def insert_missing(session, model, keys, **values):
    session.execute(insert(model).values(**values).on_conflict_do_nothing(index_elements=keys))


def seed(database_url: str):
    engine = make_engine(database_url)
    now = local_time()
    password_table = []
    try:
        with SessionLocal(bind=engine) as session, session.begin():
            for name, kind in (
                ("技术部", "技术"), ("宣传部", "宣传"), ("组织部", "组织"), ("竞赛实验室", "其他")
            ):
                insert_missing(session, Department, ["name"], name=name, type=kind,
                               created_at=now, updated_at=now)
            departments = dict(session.execute(select(Department.name, Department.id)).all())

            codes = set(OTHER_PERMISSIONS).union(*DEFAULT_PERMISSIONS.values())
            for code in sorted(codes):
                resource = code.split(":", 1)[0]
                insert_missing(session, PermissionPoint, ["code"], code=code,
                               module=MODULES[resource], description=code)
            permissions = dict(session.execute(select(PermissionPoint.code, PermissionPoint.id)).all())

            members = (
                ("admin0001", "系统管理员", "sys_admin", "技术部", "管理会员", "正式会员"),
                ("demo0001", "示例会员", "member", "技术部", "观察会员", "观察会员"),
                ("demo0002", "示例会员二", "member", "宣传部", "正式会员", "正式会员"),
                ("demo0003", "示例部门管理员", "dept_admin", "组织部", "管理会员", "正式会员"),
                ("demo0004", "示例教练", "coach", "竞赛实验室", "实验室成员", "竞赛实验室成员"),
            )
            for student_no, name, role, dept, kind, status in members:
                member = session.scalar(select(Member).where(Member.student_no == student_no))
                if member is not None:
                    password_table.append((student_no, role, "已存在，不重置"))
                    continue
                if role == "sys_admin":
                    password = os.environ.get("SEED_ADMIN_PASSWORD")
                    if password:
                        source = "SEED_ADMIN_PASSWORD"
                    elif os.environ.get("APP_ENV") == "demo":
                        print("WARNING: SEED_ADMIN_PASSWORD 未设置，使用默认管理员密码。", file=sys.stderr)
                        password = "ChangeMe123!"
                        source = "默认值"
                    else:
                        raise RuntimeError("SEED_ADMIN_PASSWORD is required unless APP_ENV=demo")
                else:
                    password = os.environ.get("SEED_DEMO_PASSWORD")
                    if password:
                        source = "SEED_DEMO_PASSWORD"
                    else:
                        password = "Demo1234!"
                        source = "默认值"
                password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
                member = Member(student_no=student_no, name=name, role=role, member_type=kind,
                                member_status=status, dept_id=departments[dept],
                                password_hash=password_hash, created_at=now, updated_at=now)
                session.add(member)
                session.flush()
                password_table.append((student_no, role, source))
                grants = permissions if role == "sys_admin" else DEFAULT_PERMISSIONS[role]
                for code in grants:
                    session.add(MemberPermission(member_id=member.id, permission_id=permissions[code],
                                                 granted_at=now))

            for key, value in (
                ("academic_year.current", f"{now[:4]}-{int(now[:4]) + 1}"),
                ("archive.lock", "false"), ("oj.sync.enabled", "false"),
            ):
                insert_missing(session, SysConfig, ["config_key"], config_key=key,
                               config_value=value, updated_at=now)
    finally:
        engine.dispose()

    print("学号 / 角色 / 密码来源", file=sys.stderr)
    for student_no, role, source in password_table:
        print(f"{student_no} / {role} / {source}", file=sys.stderr)


if __name__ == "__main__":
    seed(Settings.from_env().database_url)
    print("Seed OK")
