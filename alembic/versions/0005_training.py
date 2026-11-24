"""S4 训练：计划、阶段、任务、题目包、下发对象、打卡与逐题明细.

训练任务增加 published_at 与 status（草稿/已下发/已结束），checkin 增加 note，
task_assignee 建立 (assignee_type, assignee_id) 索引，并写入 training.alert_threshold。
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "training_plan",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("term", sa.Text(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=True),
        sa.Column("intensity", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sqlite_autoincrement=True,
    )
    op.create_index("uk_plan_term", "training_plan", ["term"], unique=True)

    op.create_table(
        "training_phase",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("training_plan.id", name="fk_phase_plan"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("start_on", sa.Text(), nullable=False),
        sa.Column("end_on", sa.Text(), nullable=False),
        sqlite_autoincrement=True,
    )
    op.create_index("idx_phase_plan", "training_phase", ["plan_id"])

    op.create_table(
        "training_task",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("phase_id", sa.Integer(), sa.ForeignKey("training_phase.id", name="fk_task_phase"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("assignee_type", sa.Text(), nullable=False),
        sa.Column("deadline_at", sa.Text(), nullable=True),
        sa.Column("published_at", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="草稿"),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("member.id", name="fk_task_creator"), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("status IN ('草稿', '已下发', '已结束')", name="ck_task_status"),
        sa.CheckConstraint(
            "assignee_type IN ('全体会员', '指定会员')", name="ck_task_assignee_type"
        ),
        sqlite_autoincrement=True,
    )
    op.create_index("idx_task_phase", "training_task", ["phase_id"])
    op.create_index("idx_task_deadline", "training_task", ["deadline_at"])

    op.create_table(
        "task_problem",
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("training_task.id", name="fk_tp_task"), primary_key=True),
        sa.Column("problem_id", sa.Integer(), sa.ForeignKey("problem.id", name="fk_tp_problem"), primary_key=True),
        sa.Column("sort_no", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("idx_tp_problem", "task_problem", ["problem_id"])

    op.create_table(
        "task_assignee",
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("training_task.id", name="fk_assignee_task"), primary_key=True),
        sa.Column("assignee_type", sa.Text(), nullable=False, primary_key=True),
        sa.Column("assignee_id", sa.Integer(), nullable=False, primary_key=True),
        sa.CheckConstraint("assignee_type = '会员'", name="ck_assignee_type"),
    )
    op.create_index("idx_assignee_target", "task_assignee", ["assignee_type", "assignee_id"])

    op.create_table(
        "checkin",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("training_task.id", name="fk_checkin_task"), nullable=False),
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("member.id", name="fk_checkin_member"), nullable=False),
        sa.Column("checkin_date", sa.Text(), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=True),
        sa.Column("solved_count", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("status IN ('打卡', '补卡', '缺卡')", name="ck_checkin_status"),
        sa.CheckConstraint("source IN ('手工', 'OJ同步')", name="ck_checkin_source"),
        sqlite_autoincrement=True,
    )
    op.create_index("uk_checkin", "checkin", ["member_id", "task_id", "checkin_date"], unique=True)
    op.create_index("idx_checkin_date", "checkin", ["checkin_date"])

    op.create_table(
        "checkin_problem",
        sa.Column("checkin_id", sa.Integer(), sa.ForeignKey("checkin.id", name="fk_cp_checkin"), primary_key=True),
        sa.Column("problem_id", sa.Integer(), sa.ForeignKey("problem.id", name="fk_cp_problem"), primary_key=True),
        sa.Column("solved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=True),
        sa.CheckConstraint("solved IN (0, 1)", name="ck_cp_solved"),
    )
    op.create_index("idx_cp_problem", "checkin_problem", ["problem_id"])

    op.create_table(
        "s4_inserted_config",
        sa.Column("config_key", sa.Text(), primary_key=True),
    )
    bind = op.get_bind()
    bind.execute(sa.text(
        "INSERT INTO s4_inserted_config (config_key) SELECT 'training.alert_threshold' "
        "WHERE NOT EXISTS (SELECT 1 FROM sys_config WHERE config_key = 'training.alert_threshold')"
    ))
    bind.execute(
        sa.text(
            "INSERT INTO sys_config (config_key, config_value, description, updated_at) "
            "VALUES ('training.alert_threshold', '0.6', '训练进度预警阈值', "
            "strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')) "
            "ON CONFLICT(config_key) DO NOTHING"
        )
    )


def downgrade():
    op.execute("DELETE FROM sys_config WHERE config_key IN (SELECT config_key FROM s4_inserted_config)")
    op.drop_table("s4_inserted_config")
    op.drop_index("idx_cp_problem", table_name="checkin_problem")
    op.drop_table("checkin_problem")
    op.drop_index("idx_checkin_date", table_name="checkin")
    op.drop_index("uk_checkin", table_name="checkin")
    op.drop_table("checkin")
    op.drop_index("idx_assignee_target", table_name="task_assignee")
    op.drop_table("task_assignee")
    op.drop_index("idx_tp_problem", table_name="task_problem")
    op.drop_table("task_problem")
    op.drop_index("idx_task_deadline", table_name="training_task")
    op.drop_index("idx_task_phase", table_name="training_task")
    op.drop_table("training_task")
    op.drop_index("idx_phase_plan", table_name="training_phase")
    op.drop_table("training_phase")
    op.drop_index("uk_plan_term", table_name="training_plan")
    op.drop_table("training_plan")
