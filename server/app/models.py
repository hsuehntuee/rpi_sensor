from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class EnvMetric(Base):
    __tablename__ = "env_metrics"
    __table_args__ = (
        UniqueConstraint("device_id", "timestamp", name="uq_env_device_timestamp"),
        Index("ix_env_device_timestamp", "device_id", "timestamp"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    temperature: Mapped[float | None] = mapped_column(Float)
    humidity: Mapped[float | None] = mapped_column(Float)
    co2_ppm: Mapped[int | None] = mapped_column(Integer)


class HVACStatus(Base):
    __tablename__ = "hvac_status"
    __table_args__ = (
        UniqueConstraint("device_id", "timestamp", name="uq_hvac_device_timestamp"),
        Index("ix_hvac_device_timestamp", "device_id", "timestamp"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    hvac_state: Mapped[int] = mapped_column(Integer, nullable=False)
    power_w: Mapped[float | None] = mapped_column(Float)


class CameraLog(Base):
    __tablename__ = "camera_logs"
    __table_args__ = (
        UniqueConstraint(
            "device_id", "timestamp", "image_type",
            name="uq_camera_device_timestamp_type",
        ),
        Index("ix_camera_device_timestamp", "device_id", "timestamp"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    image_type: Mapped[str] = mapped_column(String(8), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

