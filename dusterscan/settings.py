"""User settings and saved reports.

Settings live in %APPDATA%\\DusterScan\\settings.json (language, the Renault
database definition chosen for each module, the odometer if typed). The API
key is kept in Windows Credential Manager ("DusterScan" entry), the standard
place for secrets, which only this Windows user account can read.
Every scan is also saved to Documents\\DusterScan so scans can be compared later.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from . import i18n

APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "DusterScan"
SETTINGS_FILE = APP_DIR / "settings.json"
REPORTS_DIR = Path(os.environ.get("DUSTERSCAN_REPORTS_DIR") or Path.home() / "Documents" / "DusterScan")

# empty language = follow Windows; ddt_choices: module key -> definition file name or "generic";
# odometer_km / odometer_at: the mileage the user typed and the day they typed it (optional)
DEFAULTS = {"api_key": "", "language": "", "ddt_choices": {}, "odometer_km": None, "odometer_at": None}
_FILE_KEYS = ("language", "ddt_choices", "odometer_km", "odometer_at")

CREDENTIAL_SERVICE = "DusterScan"
CREDENTIAL_USER = "anthropic_api_key"


# -- API key (Windows Credential Manager) ----------------------------------

def _keyring():
    if sys.platform == "win32":
        # Use the Windows backend directly instead of keyring's plugin discovery,
        # which is unreliable inside a packaged .exe.
        from keyring.backends.Windows import WinVaultKeyring
        return WinVaultKeyring()
    import keyring
    return keyring.get_keyring()


def _get_api_key() -> str:
    try:
        return _keyring().get_password(CREDENTIAL_SERVICE, CREDENTIAL_USER) or ""
    except Exception:
        return ""


def _set_api_key(key: str) -> None:
    kr = _keyring()
    if key:
        kr.set_password(CREDENTIAL_SERVICE, CREDENTIAL_USER, key)
    else:
        try:
            kr.delete_password(CREDENTIAL_SERVICE, CREDENTIAL_USER)
        except Exception:
            pass


# -- settings ---------------------------------------------------------------

def _read_file() -> dict:
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def load() -> dict:
    data = {**DEFAULTS, "ddt_choices": {}}
    raw = _read_file()
    data["language"] = raw.get("language", data["language"])
    if isinstance(raw.get("ddt_choices"), dict):
        data["ddt_choices"] = raw["ddt_choices"]
    if isinstance(raw.get("odometer_km"), int) and raw["odometer_km"] > 0:
        data["odometer_km"], data["odometer_at"] = raw["odometer_km"], raw.get("odometer_at")
    data["language"] = i18n.normalize(data["language"]) if data["language"] else i18n.system_language()
    data["api_key"] = _get_api_key()
    return data


def save(values: dict) -> None:
    if "api_key" in values:
        _set_api_key(values["api_key"])
    changes = {k: values[k] for k in _FILE_KEYS if k in values}
    if changes:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        merged = {k: v for k, v in _read_file().items() if k in _FILE_KEYS}
        merged.update(changes)
        SETTINGS_FILE.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")


# -- report history -----------------------------------------------------------

def report_basename(report: dict) -> str:
    stamp = re.sub(r"[^0-9T]", "", report.get("meta", {}).get("scanned_at", ""))[:13]
    vin = report.get("vehicle", {}).get("vin") or "unknown-vin"
    return f"{stamp}_{vin}"


def save_report_files(report: dict, html: str, ai: dict | None = None) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    base = REPORTS_DIR / report_basename(report)
    base.with_suffix(".json").write_text(
        json.dumps({"report": report, "ai_analysis": ai}, ensure_ascii=False, indent=1), encoding="utf-8")
    base.with_suffix(".html").write_text(html, encoding="utf-8")
    return base.with_suffix(".html")
