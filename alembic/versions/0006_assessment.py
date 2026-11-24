"""S5 考核与转正：测评、成绩、考核记录与任务运行记录.

- assessment_test / test_score：周测月测与批量录分，test_score 按 (test_id, member_id) 唯一；
- member_assessment：考核结果按 (member_id, term) 唯一，支持连跑幂等；
- job_run：定时任务运行记录按 (job_name, biz_date, term) 唯一，attempts 自增；
- sys_config 写入 training.promote_threshold=0.6（downgrade 只删本迁移插入的键）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()

    op.create_table(
        "assessment_test",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("test_type", sa.Text(), nullable=False),
        sa.Column("held_on", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("test_type IN ('周测', '月测')", name="ck_test_type"),
        sqlite_autoincrement=True,
    )
    op.create_index("idx_test_date", "assessment_test", ["held_on"])

    op.create_table(
        "test_score",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("test_id", sa.Integer(), sa.ForeignKey("assessment_test.id", name="fk_score_test"), nullable=False),
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("member.id", name="fk_score_member"), nullable=False),
        sa.Column("solved_count", sa.Integer(), nullable=True),
        sa.Column("score", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("kp_snapshot", sa.Text(), nullable=True),
        sqlite_autoincrement=True,
    )
    op.create_index("uk_score", "test_score", ["test_id", "member_id"], unique=True)

    op.create_table(
        "member_assessment",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("member.id", name="fk_assess_member"), nullable=False),
        sa.Column("term", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("in_watchlist", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("assessed_at", sa.Text(), nullable=False),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.CheckConstraint("result IN ('通过', '不通过', '待定')", name="ck_assess_result"),
        sqlite_autoincrement=True,
    )
    op.create_index("uk_member_assessment", "member_assessment", ["member_id", "term"], unique=True)
    op.create_index("idx_assess_term", "member_assessment", ["term"])

    op.create_table(
        "job_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_name", sa.Text(), nullable=False),
        sa.Column("biz_date", sa.Text(), nullable=False),
        sa.Column("term", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('running', 'success', 'failed', 'skipped')", name="ck_job_run_status"),
        sqlite_autoincrement=True,
    )
    op.create_index("uk_job_run", "job_run", ["job_name", "biz_date", "term"], unique=True)
    op.create_index("idx_job_status", "job_run", ["status", "started_at"])

    op.create_table(
        "s5_inserted_config",
        sa.Column("config_key", sa.Text(), primary_key=True),
    )
    bind.execute(sa.text(
        "INSERT INTO s5_inserted_config (config_key) SELECT 'training.promote_threshold' "
        "WHERE NOT EXISTS (SELECT 1 FROM sys_config WHERE config_key = 'training.promote_threshold')"
    ))
    bind.execute(
        sa.text(
            "INSERT INTO sys_config (config_key, config_value, description, updated_at) "
            "VALUES ('training.promote_threshold', '0.6', '训练完成率转正阈值', "
            "strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')) "
            "ON CONFLICT(config_key) DO NOTHING"
        )
    )


def downgrade():
    op.execute("DELETE FROM sys_config WHERE config_key IN (SELECT config_key FROM s5_inserted_config)")
    op.drop_table("s5_inserted_config")
    op.drop_index("idx_job_status", table_name="job_run")
    op.drop_index("uk_job_run", table_name="job_run")
    op.drop_table("job_run")
    op.drop_index("idx_assess_term", table_name="member_assessment")
    op.drop_index("uk_member_assessment", table_name="member_assessment")
    op.drop_table("member_assessment")
    op.drop_index("uk_score", table_name="test_score")
    op.drop_table("test_score")
    op.drop_index("idx_test_date", table_name="assessment_test")
    op.drop_table("assessment_test")
