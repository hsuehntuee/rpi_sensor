from src.config import load_settings


def test_server_url_off_disables_upload(monkeypatch, tmp_path):
    monkeypatch.setenv("SERVER_URL", "OFF")
    assert load_settings(str(tmp_path / "none.env")).server_url == "off"


def test_server_url_strips_trailing_slash(monkeypatch, tmp_path):
    monkeypatch.setenv("SERVER_URL", "http://example.com:8000/")
    assert load_settings(str(tmp_path / "none.env")).server_url == "http://example.com:8000"
