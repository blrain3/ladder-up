"""Add a recoverable lease to OJ job reservations."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("job_run", sa.Column("lease_expires_at", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("job_run", "lease_expires_at")
