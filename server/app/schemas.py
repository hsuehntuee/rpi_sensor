from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EnvMetricIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    temperature: float | None = None
    humidity: float | None = Field(default=None, ge=0, le=100)
    co2_ppm: int | None = Field(default=None, ge=0)

    @field_validator("timestamp")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        return value


class HVACStatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    hvac_state: int = Field(ge=-1, le=1)
    power_w: float | None = Field(default=None, ge=0)

    @field_validator("timestamp")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        return value


class MetricsSyncIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    sync_timestamp: datetime
    env_data: list[EnvMetricIn] = Field(default_factory=list, max_length=5000)
    hvac_data: list[HVACStatusIn] = Field(default_factory=list, max_length=5000)

    @field_validator("sync_timestamp")
    @classmethod
    def require_sync_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("sync_timestamp must include a timezone")
        return value


class SyncResult(BaseModel):
    env_inserted: int
    hvac_inserted: int


class ImageResult(BaseModel):
    id: int
    stored: bool
    file_path: str
