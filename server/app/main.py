from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_session
from .models import CameraLog, EnvMetric, HVACStatus
from .schemas import ImageResult, MetricsSyncIn, SyncResult
from .security import require_api_key

app = FastAPI(title="Raspberry Pi Sensor Server", version="1.0.0")
SessionDependency = Annotated[Session, Depends(get_session)]
AuthDependency = Annotated[None, Depends(require_api_key)]


@app.get("/health")
def health(session: SessionDependency) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.post("/api/v1/sync/metrics", response_model=SyncResult)
def sync_metrics(
    payload: MetricsSyncIn,
    session: SessionDependency,
    _: AuthDependency,
) -> SyncResult:
    env_values = [
        {"device_id": payload.device_id, **item.model_dump()}
        for item in payload.env_data
    ]
    hvac_values = [
        {"device_id": payload.device_id, **item.model_dump()}
        for item in payload.hvac_data
    ]
    env_inserted = 0
    hvac_inserted = 0
    if env_values:
        result = session.execute(
            insert(EnvMetric)
            .values(env_values)
            .on_conflict_do_nothing(
                constraint="uq_env_device_timestamp"
            )
            .returning(EnvMetric.id)
        )
        env_inserted = len(result.scalars().all())
    if hvac_values:
        result = session.execute(
            insert(HVACStatus)
            .values(hvac_values)
            .on_conflict_do_nothing(
                constraint="uq_hvac_device_timestamp"
            )
            .returning(HVACStatus.id)
        )
        hvac_inserted = len(result.scalars().all())
    session.commit()
    return SyncResult(env_inserted=env_inserted, hvac_inserted=hvac_inserted)


@app.post(
    "/api/v1/sync/images",
    response_model=ImageResult,
    status_code=status.HTTP_201_CREATED,
)
def upload_image(
    session: SessionDependency,
    _: AuthDependency,
    device_id: Annotated[
        str,
        Form(
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        ),
    ],
    timestamp: Annotated[datetime, Form()],
    image_type: Annotated[str, Form(pattern="^(RGB|IR)$")],
    image: Annotated[UploadFile, File()],
) -> ImageResult:
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    existing = session.scalar(
        select(CameraLog).where(
            CameraLog.device_id == device_id,
            CameraLog.timestamp == timestamp,
            CameraLog.image_type == image_type,
        )
    )
    if existing is not None:
        return ImageResult(
            id=existing.id, stored=False, file_path=existing.file_path
        )

    settings = get_settings()
    storage = settings.image_storage_path / device_id
    storage.mkdir(parents=True, exist_ok=True)
    suffix = Path(image.filename or "").suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".tiff", ".tif"}:
        suffix = ".bin"
    final_path = storage / f"{timestamp:%Y%m%dT%H%M%S}_{image_type}_{uuid4().hex}{suffix}"
    temporary_path = final_path.with_suffix(final_path.suffix + ".part")
    size = 0
    try:
        with temporary_path.open("xb") as output:
            while chunk := image.file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_image_bytes:
                    raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "image too large")
                output.write(chunk)
        if size == 0:
            raise HTTPException(422, "image is empty")
        os.replace(temporary_path, final_path)
        row = CameraLog(
            device_id=device_id,
            timestamp=timestamp,
            image_type=image_type,
            file_path=str(final_path),
            content_type=image.content_type or "application/octet-stream",
            size_bytes=size,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
    except Exception:
        session.rollback()
        temporary_path.unlink(missing_ok=True)
        final_path.unlink(missing_ok=True)
        raise
    return ImageResult(id=row.id, stored=True, file_path=row.file_path)
