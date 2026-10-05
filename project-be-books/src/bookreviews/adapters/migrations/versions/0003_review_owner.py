import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reviews", sa.Column("owner", sa.String(length=64), nullable=True))
    op.execute("UPDATE reviews SET owner = 'anonymous'")
    op.alter_column("reviews", "owner", existing_type=sa.String(length=64), nullable=False)


def downgrade() -> None:
    op.drop_column("reviews", "owner")
