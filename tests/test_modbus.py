from unittest.mock import Mock

import pytest

from src.control.hvac_modbus import HVACCommunicationError, HVACModbus


def response(value=1, error=False):
    result = Mock()
    result.registers = [value]
    result.isError.return_value = error
    return result


def test_read_and_control_modbus():
    client = Mock()
    client.read_holding_registers.side_effect = [response(1), response(1200)]
    client.write_register.return_value = response()
    hvac = HVACModbus(client, 7, 100, 101, 102)

    assert hvac.read_status() == {"hvac_state": 1, "power_w": 1200.0}
    hvac.set_power(True)
    client.write_register.assert_called_once_with(101, 1, slave=7)


def test_modbus_error_is_wrapped():
    client = Mock()
    client.read_holding_registers.return_value = response(error=True)
    with pytest.raises(HVACCommunicationError):
        HVACModbus(client, 7, 100, 101).read_status()

