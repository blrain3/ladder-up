"""S6 奖惩：member_award 表.

- extra_points 为整数百分位（150 表示 1.50 分），可为负；
- 唯一索引 (member_id, happened_on, title) 拦截同会员同一天同标题重复录入；
- award_type 仅允许 奖励 / 惩处，happened_on 为 YYYY-MM-DD 文本。
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "member_award",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("member.id", name="fk_award_member"), nullable=False),
        sa.Column("award_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("happened_on", sa.Text(), nullable=False),
        sa.Column("extra_points", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("award_type IN ('奖励', '惩处')", name="ck_award_type"),
        sqlite_autoincrement=True,
    )
    op.create_index("idx_award_member", "member_award", ["member_id"])
    op.create_index("uk_award_member_day_title", "member_award", ["member_id", "happened_on", "title"], unique=True)


def downgrade():
    op.drop_index("uk_award_member_day_title", table_name="member_award")
    op.drop_index("idx_award_member", table_name="member_award")
    op.drop_table("member_award")
