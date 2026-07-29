"""Initial sensor schema."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "env_metrics",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=True),
        sa.Column("humidity", sa.Float(), nullable=True),
        sa.Column("co2_ppm", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "device_id", "timestamp", name="uq_env_device_timestamp"
        ),
    )
    op.create_index(
        "ix_env_device_timestamp",
        "env_metrics",
        ["device_id", "timestamp"],
    )
    op.create_table(
        "hvac_status",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hvac_state", sa.Integer(), nullable=False),
        sa.Column("power_w", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "device_id", "timestamp", name="uq_hvac_device_timestamp"
        ),
    )
    op.create_index(
        "ix_hvac_device_timestamp",
        "hvac_status",
        ["device_id", "timestamp"],
    )
    op.create_table(
        "camera_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("image_type", sa.String(length=8), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "device_id",
            "timestamp",
            "image_type",
            name="uq_camera_device_timestamp_type",
        ),
    )
    op.create_index(
        "ix_camera_device_timestamp",
        "camera_logs",
        ["device_id", "timestamp"],
    )


def downgrade() -> None:
    op.drop_index("ix_camera_device_timestamp", table_name="camera_logs")
    op.drop_table("camera_logs")
    op.drop_index("ix_hvac_device_timestamp", table_name="hvac_status")
    op.drop_table("hvac_status")
    op.drop_index("ix_env_device_timestamp", table_name="env_metrics")
    op.drop_table("env_metrics")

