from __future__ import annotations

from typing import Any


class HVACCommunicationError(RuntimeError):
    pass


class HVACModbus:
    def __init__(
        self,
        client: Any,
        slave_id: int,
        state_register: int,
        control_register: int,
        power_register: int | None = None,
    ) -> None:
        self.client = client
        self.slave_id = slave_id
        self.state_register = state_register
        self.control_register = control_register
        self.power_register = power_register

    def _ensure_ok(self, response: Any) -> Any:
        if response is None or (
            callable(getattr(response, "isError", None)) and response.isError()
        ):
            raise HVACCommunicationError("Modbus device returned an error")
        return response

    def read_status(self) -> dict[str, int | float | None]:
        try:
            state_response = self._ensure_ok(
                self.client.read_holding_registers(
                    self.state_register, count=1, slave=self.slave_id
                )
            )
            power = None
            if self.power_register is not None:
                power_response = self._ensure_ok(
                    self.client.read_holding_registers(
                        self.power_register, count=1, slave=self.slave_id
                    )
                )
                power = float(power_response.registers[0])
            return {
                "hvac_state": int(state_response.registers[0]),
                "power_w": power,
            }
        except HVACCommunicationError:
            raise
        except Exception as exc:
            raise HVACCommunicationError("Modbus status read failed") from exc

    def set_power(self, enabled: bool) -> None:
        try:
            response = self.client.write_register(
                self.control_register, int(enabled), slave=self.slave_id
            )
            self._ensure_ok(response)
        except HVACCommunicationError:
            raise
        except Exception as exc:
            raise HVACCommunicationError("Modbus power command failed") from exc

