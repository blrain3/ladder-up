"""Bind CSRF tokens to server-side sessions."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("member_session", sa.Column("csrf_nonce", sa.Text()))
    op.execute("UPDATE member_session SET csrf_nonce = lower(hex(randomblob(32)))")


def downgrade():
    op.drop_column("member_session", "csrf_nonce")
