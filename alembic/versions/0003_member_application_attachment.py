"""S2 入会：补齐会员、申请与附件的可迁移字段."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

REPAIRS = (
    ("member", "must_change_password", sa.Integer()),
    ("member_application", "interviewed_at", sa.Text()),
    ("member_application", "active_flag", sa.Text()),
    ("attachment", "biz_id", sa.Integer()),
    ("attachment", "deleted_at", sa.Text()),
)
REPAIR_TABLE = "_s2_schema_repairs"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    repairs = []
    for table, column, kind in REPAIRS:
        if column in {item["name"] for item in inspector.get_columns(table)}:
            continue
        options = {"nullable": False, "server_default": "0"} if column == "must_change_password" else {}
        op.add_column(table, sa.Column(column, kind, **options))
        repairs.append((table, column))

    op.execute("""
        UPDATE member_application SET active_flag = '1'
        WHERE active_flag IS NULL AND status IN ('待审核', '待面试', '已面试')
    """)
    if not any(index["name"] == "uk_app_active" for index in inspector.get_indexes("member_application")):
        op.create_index(
            "uk_app_active", "member_application", ["student_no", "apply_type", "active_flag"],
            unique=True,
        )
        repairs.append(("member_application", "uk_app_active"))

    op.add_column("member_application", sa.Column("receipt_code_hash", sa.Text()))
    op.create_index("uk_app_receipt_code_hash", "member_application", ["receipt_code_hash"], unique=True)

    if repairs:
        tracking = op.create_table(
            REPAIR_TABLE, sa.Column("table_name", sa.Text(), primary_key=True),
            sa.Column("item_name", sa.Text(), primary_key=True),
        )
        op.bulk_insert(tracking, [{"table_name": table, "item_name": item} for table, item in repairs])


def downgrade():
    op.drop_index("uk_app_receipt_code_hash", table_name="member_application")
    op.drop_column("member_application", "receipt_code_hash")
    bind = op.get_bind()
    if sa.inspect(bind).has_table(REPAIR_TABLE):
        repairs = {tuple(row) for row in bind.execute(
            sa.text(f"SELECT table_name, item_name FROM {REPAIR_TABLE}")
        )}
        if ("member_application", "uk_app_active") in repairs:
            op.drop_index("uk_app_active", table_name="member_application")
        for table, column, _ in REPAIRS:
            if (table, column) in repairs:
                op.drop_column(table, column)
        op.drop_table(REPAIR_TABLE)
