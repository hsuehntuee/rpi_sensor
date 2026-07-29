from src.sensors.camera_ir import LeptonCCI, LeptonDetectionError


def words(text: str) -> list[int]:
    raw = text.encode("ascii").ljust(32, b"\x00")
    return [int.from_bytes(raw[index:index + 2], "big") for index in range(0, 32, 2)]


class Transport:
    def __init__(self, part_number: str):
        self.part_number = part_number
        self.writes = []

    def read_words(self, register: int, count: int):
        if register == LeptonCCI.STATUS_REGISTER:
            return [0]
        return words(self.part_number)[:count]

    def write_word(self, register: int, value: int):
        self.writes.append((register, value))


def test_detects_lepton_25():
    transport = Transport("500-0763-01")
    model = LeptonCCI(transport).detect_model()
    assert (model.family, model.width, model.height) == ("Lepton 2.5", 80, 60)
    assert transport.writes == [
        (LeptonCCI.DATA_LENGTH_REGISTER, 16),
        (LeptonCCI.COMMAND_REGISTER, LeptonCCI.OEM_FLIR_PART_NUMBER_GET),
    ]


def test_detects_lepton_35():
    model = LeptonCCI(Transport("500-0771-01")).detect_model()
    assert (model.family, model.width, model.height) == ("Lepton 3.5", 160, 120)


def test_unknown_part_number_is_not_guessed():
    try:
        LeptonCCI(Transport("unknown")).detect_model()
    except LeptonDetectionError as error:
        assert "LEPTON_WIDTH" in str(error)
    else:
        raise AssertionError("unknown model must fail")


def test_unknown_part_number_accepts_explicit_dimensions():
    model = LeptonCCI(Transport("custom-core")).detect_model(320, 240)
    assert model.family == "Unknown Lepton (manual dimensions)"
    assert (model.width, model.height) == (320, 240)


from unittest.mock import Mock, patch
from src.sensors.camera_ir import LeptonVoSPI


@patch("src.sensors.camera_ir.spidev")
def test_lepton_vospi_reads_frame_lepton_25(mock_spidev):
    mock_spi = Mock()
    mock_spidev.SpiDev.return_value = mock_spi

    vospi = LeptonVoSPI(spi_bus=0, spi_device=0, width=80, height=60)
    vospi.spi = mock_spi

    mock_packets = []
    for i in range(60):
        packet = [0] * 164
        packet[0] = 0x00
        packet[1] = i
        packet[4:] = [i] * 160
        mock_packets.append(packet)

    mock_spi.readbytes.side_effect = mock_packets

    frame = vospi.read_frame()
    assert frame.shape == (60, 80)
    for i in range(60):
        expected_val = (i << 8) | i
        assert frame[i, 0] == expected_val


@patch("src.sensors.camera_ir.spidev")
def test_lepton_vospi_reads_frame_lepton_35(mock_spidev):
    mock_spi = Mock()
    mock_spidev.SpiDev.return_value = mock_spi

    vospi = LeptonVoSPI(spi_bus=0, spi_device=0, width=160, height=120)
    vospi.spi = mock_spi

    mock_packets = []
    for seg in range(1, 5):
        for i in range(60):
            packet = [0] * 164
            if i == 20:
                packet[0] = (seg & 0x07) << 4
            else:
                packet[0] = 0x00
            packet[1] = i
            packet[4:] = [seg * 10 + i] * 160
            mock_packets.append(packet)

    mock_spi.readbytes.side_effect = mock_packets

    frame = vospi.read_frame()
    assert frame.shape == (120, 160)

    expected_p0 = (10 << 8) | 10
    assert frame[0, 0] == expected_p0

    expected_p1 = (11 << 8) | 11
    assert frame[0, 80] == expected_p1
