"""S7 OJ accounts and submissions."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "oj_account",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("member.id", name="fk_oj_member"), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("handle", sa.Text(collation="NOCASE"), nullable=False),
        sa.Column("verified_at", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sqlite_autoincrement=True,
    )
    op.create_index("uk_oj_account", "oj_account", ["platform", "handle"], unique=True)
    op.create_index("uk_oj_member_platform", "oj_account", ["member_id", "platform"], unique=True)
    op.create_index("idx_oj_member", "oj_account", ["member_id"])
    op.create_table(
        "oj_submission",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("oj_account_id", sa.Integer(), sa.ForeignKey(
            "oj_account.id", name="fk_sub_account", ondelete="SET NULL",
        )),
        sa.Column("problem_id", sa.Integer(), sa.ForeignKey("problem.id", name="fk_sub_problem")),
        sa.Column("verdict", sa.Text()),
        sa.Column("submitted_at", sa.Text(), nullable=False),
        sa.Column("synced_at", sa.Text(), nullable=False),
        sqlite_autoincrement=True,
    )
    op.create_index("uk_oj_submission", "oj_submission", ["platform", "external_id"], unique=True)
    op.create_index("idx_sub_time", "oj_submission", ["submitted_at"])
    op.create_index("idx_sub_account", "oj_submission", ["oj_account_id"])

    op.create_table("s7_inserted_config", sa.Column("config_key", sa.Text(), primary_key=True))
    bind = op.get_bind()
    for key, value, description in (
        ("oj.sync.enabled", "false", "启用 Codeforces 在线同步"),
        ("oj.sync.limit", "100", "每个 Codeforces 账号最多拉取的提交数"),
    ):
        bind.execute(sa.text(
            "INSERT INTO s7_inserted_config SELECT :key "
            "WHERE NOT EXISTS (SELECT 1 FROM sys_config WHERE config_key = :key)"
        ), {"key": key})
        bind.execute(sa.text(
            "INSERT INTO sys_config (config_key, config_value, description, updated_at) "
            "VALUES (:key, :value, :description, datetime('now', '+8 hours')) "
            "ON CONFLICT(config_key) DO NOTHING"
        ), {"key": key, "value": value, "description": description})


def downgrade():
    op.execute("DELETE FROM sys_config WHERE config_key IN (SELECT config_key FROM s7_inserted_config)")
    op.drop_table("s7_inserted_config")
    op.drop_table("oj_submission")
    op.drop_table("oj_account")
