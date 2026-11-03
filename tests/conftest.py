import pytest

from dusterscan import settings
from dusterscan.ddt import store


@pytest.fixture(autouse=True)
def isolated_user_folders(tmp_path, monkeypatch):
    """Tests never see (or change) the real settings, imported database or saved reports."""
    monkeypatch.setattr(settings, "APP_DIR", tmp_path / "appdata")
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "appdata" / "settings.json")
    monkeypatch.setattr(settings, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(store, "DDT_DIR", tmp_path / "ddt")
    store._load_imported.cache_clear()
