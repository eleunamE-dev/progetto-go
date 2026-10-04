import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reviews", sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False)
    )
    op.create_table(
        "idempotency_keys",
        sa.Column("owner", sa.String(length=64), nullable=False),
        sa.Column(
            "idempotency_key",
            mysql.VARCHAR(length=255, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("review_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["reviews.id"],
            name="fk_idempotency_keys_review_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("owner", "idempotency_key"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index("ix_idempotency_keys_created_at", "idempotency_keys", ["created_at"])


def downgrade() -> None:
    op.drop_table("idempotency_keys")
    op.drop_column("reviews", "version")
