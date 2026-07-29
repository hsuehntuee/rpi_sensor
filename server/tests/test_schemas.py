from datetime import datetime

import pytest
from pydantic import ValidationError

from app.schemas import MetricsSyncIn


def test_metrics_payload_matches_edge_contract():
    payload = MetricsSyncIn.model_validate(
        {
            "device_id": "rpi_agri_304D",
            "sync_timestamp": "2026-07-24T23:15:00Z",
            "env_data": [
                {
                    "timestamp": "2026-07-24T23:14:00Z",
                    "temperature": 25.4,
                    "humidity": 60.1,
                    "co2_ppm": 450,
                }
            ],
            "hvac_data": [],
        }
    )
    assert payload.env_data[0].timestamp == datetime.fromisoformat(
        "2026-07-24T23:14:00+00:00"
    )


def test_naive_metric_timestamp_is_rejected():
    with pytest.raises(ValidationError):
        MetricsSyncIn.model_validate(
            {
                "device_id": "device",
                "sync_timestamp": "2026-07-24T23:15:00Z",
                "env_data": [{"timestamp": "2026-07-24T23:14:00"}],
            }
        )

