"""Create S1 tables, including the application schema required by batch one."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def text(name, *, nullable=False, default=None):
    return sa.Column(name, sa.Text(), nullable=nullable, server_default=default)


def integer(name, *, nullable=False):
    return sa.Column(name, sa.Integer(), nullable=nullable)


def reference(name, target, constraint, *, nullable=False):
    return sa.Column(name, sa.Integer(), sa.ForeignKey(target, name=constraint), nullable=nullable)


def create_table(name, *columns):
    op.create_table(
        name, sa.Column("id", sa.Integer(), primary_key=True), *columns,
        sqlite_autoincrement=True,
    )


def upgrade():
    create_table("department", text("name"), text("type"), text("created_at"), text("updated_at"))
    op.create_index("uk_dept_name", "department", ["name"], unique=True)

    # SQLite accepts forward references, so both sides of the photo cycle can
    # be declared inline with foreign key enforcement enabled throughout.
    create_table(
        "member",
        text("student_no"), text("name"), text("major", nullable=True),
        integer("grade", nullable=True), text("phone", nullable=True), text("email", nullable=True),
        text("password_hash"), text("auth_provider", default="local"),
        reference("photo_attachment_id", "attachment.id", "fk_member_photo", nullable=True),
        text("member_type"), text("role", default="member"), text("member_status"),
        reference("dept_id", "department.id", "fk_member_dept", nullable=True),
        text("joined_on", nullable=True), text("left_on", nullable=True),
        text("created_at"), text("updated_at"), text("deleted_at", nullable=True),
    )
    op.create_index("uk_member_student_no", "member", ["student_no"], unique=True)
    op.create_index("idx_member_status", "member", ["member_status"])
    op.create_index("idx_member_role", "member", ["role"])
    op.create_index("idx_member_dept", "member", ["dept_id"])

    create_table(
        "attachment",
        text("object_key"),
        reference("owner_member_id", "member.id", "fk_attachment_owner", nullable=True),
        text("biz_type"), integer("biz_id", nullable=True), text("mime"), integer("byte_size"),
        text("sha256"), text("created_at"), text("deleted_at", nullable=True),
    )
    op.create_index("uk_attachment_key", "attachment", ["object_key"], unique=True)
    op.create_index("idx_attachment_owner", "attachment", ["owner_member_id"])
    op.create_index("idx_attachment_biz", "attachment", ["biz_type", "biz_id"])

    create_table("permission_point", text("code"), text("module"), text("description"))
    op.create_index("uk_perm_code", "permission_point", ["code"], unique=True)
    create_table(
        "member_permission",
        reference("member_id", "member.id", "fk_mp_member"),
        reference("permission_id", "permission_point.id", "fk_mp_perm"),
        text("granted_at"), integer("granted_by", nullable=True),
    )
    op.create_index("uk_member_perm", "member_permission", ["member_id", "permission_id"], unique=True)

    create_table(
        "member_session",
        reference("member_id", "member.id", "fk_session_member"),
        text("token_hash"), text("issued_at"), text("expires_at"), text("revoked_at", nullable=True),
        text("revoke_reason", nullable=True), text("user_agent", nullable=True), text("ip", nullable=True),
    )
    op.create_index("uk_session_token", "member_session", ["token_hash"], unique=True)
    op.create_index("idx_session_member", "member_session", ["member_id", "expires_at"])

    create_table(
        "member_application",
        text("student_no"), text("name"), text("major", nullable=True),
        integer("grade", nullable=True), text("phone", nullable=True), text("email", nullable=True),
        text("apply_type"), text("status"), text("active_flag", nullable=True),
        text("interview_note", nullable=True), text("interviewed_at", nullable=True),
        text("review_note", nullable=True),
        reference("reviewed_by", "member.id", "fk_app_reviewer", nullable=True),
        text("reviewed_at", nullable=True),
        reference("member_id", "member.id", "fk_app_member", nullable=True),
        text("created_at"),
        sa.CheckConstraint("active_flag = '1'", name="ck_app_active_flag"),
    )
    op.create_index("idx_app_status", "member_application", ["status", "created_at"])
    op.create_index(
        "uk_app_active", "member_application", ["student_no", "apply_type", "active_flag"],
        unique=True,
    )

    create_table(
        "audit_log",
        integer("actor_id", nullable=True), text("action"), text("target_type"),
        integer("target_id", nullable=True), text("before_json", nullable=True),
        text("after_json", nullable=True), text("ip", nullable=True), text("request_id", nullable=True),
        text("created_at"),
        sa.CheckConstraint("before_json IS NULL OR json_valid(before_json)", name="ck_audit_before_json"),
        sa.CheckConstraint("after_json IS NULL OR json_valid(after_json)", name="ck_audit_after_json"),
    )
    op.create_index("idx_audit_actor", "audit_log", ["actor_id"])
    op.create_index("idx_audit_target", "audit_log", ["target_type", "target_id"])
    op.create_index("idx_audit_time", "audit_log", ["created_at"])

    op.create_table(
        "sys_config",
        sa.Column("config_key", sa.Text(), primary_key=True),
        text("config_value", nullable=True), text("description", nullable=True), text("updated_at"),
    )


def downgrade():
    for table in ("sys_config", "audit_log", "member_application", "member_session", "member_permission"):
        op.drop_table(table)
    op.execute("UPDATE member SET photo_attachment_id = NULL WHERE photo_attachment_id IS NOT NULL")
    op.execute("DELETE FROM attachment")
    op.execute("DELETE FROM member")
    op.drop_table("attachment")
    op.drop_table("permission_point")
    op.drop_table("member")
    op.drop_table("department")
