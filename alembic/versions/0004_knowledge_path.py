"""S3 题库：知识点树与题目、题解表.

知识点同父唯一采用 0 哨兵表达式索引：
    uk_kp_parent_name = UNIQUE (coalesce(parent_id, 0), name)
这样根节点（parent_id 为 NULL）也能与同层根节点互斥，而不是用两张部分索引。
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "knowledge_point",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("knowledge_point.id", name="fk_kp_parent"), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("difficulty", sa.Text(), nullable=False, server_default="入门"),
        sa.Column("depth", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("path", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Text(), nullable=False),
        sqlite_autoincrement=True,
    )
    op.create_index("idx_kp_parent", "knowledge_point", ["parent_id"])
    # 0 哨兵表达式索引：parent_id 为 NULL 的根节点用 0 参与唯一约束。
    op.execute(
        "CREATE UNIQUE INDEX uk_kp_parent_name "
        "ON knowledge_point (coalesce(parent_id, 0), name)"
    )

    op.create_table(
        "problem",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("platform", sa.Text(), nullable=True),
        sa.Column("difficulty", sa.Text(), nullable=False),
        sa.Column("problem_type", sa.Text(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=True),
        sa.Column("sample_in", sa.Text(), nullable=True),
        sa.Column("sample_out", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("member.id", name="fk_problem_creator"), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("deleted_at", sa.Text(), nullable=True),
        sqlite_autoincrement=True,
    )
    op.execute(
        "CREATE UNIQUE INDEX platform_external_uk ON problem (platform, external_id) "
        "WHERE platform IS NOT NULL AND external_id IS NOT NULL"
    )
    op.create_index("idx_problem_difficulty", "problem", ["difficulty"])
    op.create_index("idx_problem_type", "problem", ["problem_type"])

    op.create_table(
        "problem_kp",
        sa.Column("problem_id", sa.Integer(), sa.ForeignKey("problem.id", name="fk_pk_problem"), primary_key=True),
        sa.Column("kp_id", sa.Integer(), sa.ForeignKey("knowledge_point.id", name="fk_pk_kp"), primary_key=True),
    )
    op.create_index("idx_pk_kp", "problem_kp", ["kp_id"])

    op.create_table(
        "problem_solution",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("problem_id", sa.Integer(), sa.ForeignKey("problem.id", name="fk_solution_problem"), nullable=False),
        sa.Column("author_id", sa.Integer(), sa.ForeignKey("member.id", name="fk_solution_author"), nullable=True),
        sa.Column("content_md", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sqlite_autoincrement=True,
    )
    op.create_index("idx_solution_problem", "problem_solution", ["problem_id"])

    # 为已有行回填（新表通常为空，这里兼容从旧结构升级而来的数据）：
    # 根节点 depth=1、path='/'||id||'/'，子节点按其父链逐层推导。
    bind = op.get_bind()
    bind.execute(sa.text(
        "UPDATE knowledge_point SET depth = 1, path = '/' || id || '/' WHERE parent_id IS NULL"
    ))
    for _ in range(bind.execute(sa.text("SELECT count(*) FROM knowledge_point")).scalar() + 1):
        result = bind.execute(sa.text(
            "UPDATE knowledge_point "
            "SET depth = (SELECT p.depth + 1 FROM knowledge_point p WHERE p.id = knowledge_point.parent_id), "
            "    path = (SELECT p.path || knowledge_point.id || '/' FROM knowledge_point p WHERE p.id = knowledge_point.parent_id) "
            "WHERE parent_id IS NOT NULL AND depth = 1 AND path = '' "
            "AND (SELECT p.depth FROM knowledge_point p WHERE p.id = knowledge_point.parent_id) IS NOT NULL"
        ))
        if result.rowcount == 0:
            break


def downgrade():
    op.drop_index("idx_solution_problem", table_name="problem_solution")
    op.drop_table("problem_solution")
    op.drop_index("idx_pk_kp", table_name="problem_kp")
    op.drop_table("problem_kp")
    op.drop_index("idx_problem_difficulty", table_name="problem")
    op.drop_index("idx_problem_type", table_name="problem")
    op.execute("DROP INDEX IF EXISTS platform_external_uk")
    op.drop_table("problem")
    op.execute("DROP INDEX IF EXISTS uk_kp_parent_name")
    op.drop_index("idx_kp_parent", table_name="knowledge_point")
    op.drop_table("knowledge_point")
