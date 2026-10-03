import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

OLD_STATUS = sa.Enum("pending", "completed", name="review_status")
NEW_STATUS = sa.Enum("pending", "completed", "failed", name="review_status")


def upgrade() -> None:
    op.create_table(
        "books",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("authors", sa.JSON(), nullable=False),
        sa.Column("subjects", sa.JSON(), nullable=False),
        sa.Column("bookshelves", sa.JSON(), nullable=False),
        sa.Column("languages", sa.JSON(), nullable=False),
        sa.Column("summaries", sa.JSON(), nullable=False),
        sa.Column("cover_url", sa.String(length=2048), nullable=True),
        sa.Column("download_count", sa.Integer(), nullable=False),
        sa.Column("fetched_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.add_column("reviews", sa.Column("queued_at", mysql.DATETIME(fsp=6), nullable=True))
    op.add_column("reviews", sa.Column("processed_at", mysql.DATETIME(fsp=6), nullable=True))
    op.execute("UPDATE reviews SET queued_at = created_at")
    op.alter_column("reviews", "queued_at", existing_type=mysql.DATETIME(fsp=6), nullable=False)
    op.alter_column(
        "reviews", "status", existing_type=OLD_STATUS, type_=NEW_STATUS, existing_nullable=False
    )
    op.create_index("ix_reviews_status_queued_at", "reviews", ["status", "queued_at"])


def downgrade() -> None:
    op.drop_index("ix_reviews_status_queued_at", table_name="reviews")
    op.execute("UPDATE reviews SET status = 'pending' WHERE status = 'failed'")
    op.alter_column(
        "reviews", "status", existing_type=NEW_STATUS, type_=OLD_STATUS, existing_nullable=False
    )
    op.drop_column("reviews", "processed_at")
    op.drop_column("reviews", "queued_at")
    op.drop_table("books")
