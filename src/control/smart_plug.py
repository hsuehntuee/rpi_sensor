from __future__ import annotations

from typing import Protocol


class SmartPlug(Protocol):
    def set_power(self, enabled: bool) -> None: ...
    def read_status(self) -> dict[str, int | float | None]: ...

