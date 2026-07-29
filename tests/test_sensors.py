from unittest.mock import Mock

from src.sensors.scd41 import SCD41Sensor


def test_scd41_only_returns_fresh_measurements():
    driver = Mock()
    driver.data_ready = False
    sensor = SCD41Sensor(driver)
    assert sensor.read() is None

    driver.data_ready = True
    driver.CO2 = 850
    driver.temperature = 25.0
    driver.relative_humidity = 55.0
    assert sensor.read() == {
        "co2_ppm": 850,
        "temperature": 25.0,
        "humidity": 55.0,
    }
    driver.start_periodic_measurement.assert_called_once()
