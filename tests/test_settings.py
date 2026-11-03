"""Settings: language in settings.json, API key in the OS credential store (faked here)."""

import json

from dusterscan import settings


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, value):
        self.store[(service, user)] = value

    def delete_password(self, service, user):
        self.store.pop((service, user))


def test_api_key_goes_to_credential_store_not_file(tmp_path, monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(settings, "_keyring", lambda: fake)
    monkeypatch.setattr(settings, "APP_DIR", tmp_path)
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")

    settings.save({"api_key": "sk-test-123"})
    settings.save({"language": "es"})
    assert settings.load() == {"api_key": "sk-test-123", "language": "es", "ddt_choices": {},
                               "odometer_km": None, "odometer_at": None}
    assert "sk-test" not in (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert json.loads((tmp_path / "settings.json").read_text(encoding="utf-8")) == {"language": "es"}
    # saving one setting keeps the others
    settings.save({"ddt_choices": {"740|8|037|4569|0170": "generic"}})
    assert settings.load()["language"] == "es"
    assert settings.load()["ddt_choices"] == {"740|8|037|4569|0170": "generic"}

    settings.save({"api_key": ""})
    assert settings.load()["api_key"] == ""
    assert fake.store == {}
