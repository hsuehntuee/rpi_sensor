from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class SensorReadError(RuntimeError):
    pass


class BaseSensor(ABC):
    @abstractmethod
    def read(self) -> dict[str, Any]:
        """Read and return normalized sensor values."""

