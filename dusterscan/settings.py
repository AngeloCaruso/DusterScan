"""User settings and saved reports.

Settings live in %APPDATA%\\DusterScan\\settings.json (language only). The API
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

DEFAULTS = {"api_key": "", "language": ""}  # empty language = follow Windows

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

def load() -> dict:
    data = dict(DEFAULTS)
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        data["language"] = raw.get("language", data["language"])
    except (OSError, ValueError):
        pass
    data["language"] = i18n.normalize(data["language"]) if data["language"] else i18n.system_language()
    data["api_key"] = _get_api_key()
    return data


def save(values: dict) -> None:
    if "api_key" in values:
        _set_api_key(values["api_key"])
    if "language" in values:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps({"language": values["language"]}, indent=2), encoding="utf-8")


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
