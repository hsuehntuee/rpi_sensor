from __future__ import annotations

from typing import Any

from .base_sensor import BaseSensor, SensorReadError


class SCD41Sensor(BaseSensor):
    """Adapter for an Adafruit-compatible SCD41 driver.

    The sensor is polled frequently, but values are returned only when the
    hardware reports a new measurement (normally every five seconds).
    """

    def __init__(self, driver: Any) -> None:
        self.driver = driver
        self._started = False

    def initialize(self) -> None:
        try:
            self.driver.start_periodic_measurement()
            self._started = True
        except Exception as exc:
            raise SensorReadError("SCD41 initialization failed") from exc

    def read(self) -> dict[str, float | int] | None:
        if not self._started:
            self.initialize()
        try:
            if not bool(self.driver.data_ready):
                return None
            return {
                "co2_ppm": int(self.driver.CO2),
                "temperature": float(self.driver.temperature),
                "humidity": float(self.driver.relative_humidity),
            }
        except Exception as exc:
            raise SensorReadError("SCD41 read failed") from exc
