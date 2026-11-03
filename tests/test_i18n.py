"""Every language is complete, and every key the code uses exists."""

import json
import re
from pathlib import Path

import pytest

from dusterscan import i18n, obd2, settings
from dusterscan.renault import KNOWN_ECUS, RENAULT_IDENT_FIELDS, UDS_IDENT_DIDS

PKG = Path(__file__).resolve().parent.parent / "dusterscan"
EN = json.loads((PKG / "locales" / "en.json").read_text(encoding="utf-8"))


def placeholders(s: str) -> set[str]:
    return set(re.findall(r"\{(\w+)", s))


@pytest.mark.parametrize("lang", list(i18n.LANGUAGES))
def test_language_file_is_complete(lang):
    data = json.loads((PKG / "locales" / f"{lang}.json").read_text(encoding="utf-8"))
    assert set(data) == set(EN), sorted(set(EN) ^ set(data))
    for key, value in data.items():
        assert placeholders(value) == placeholders(EN[key]), key
        assert value.strip() or key == "finding.ftb_suffix", f"{lang}: empty text for {key}"


def test_literal_keys_in_code_exist():
    prefixes = ("ui|error|progress|log|finding|report|note|nrc|state|dtc_kind|system|severity|scan_type|"
                "drive|urgency|diy|likelihood|ident|ecu|pidnote|lang|vehicle")
    pattern = re.compile(rf"""["']((?:{prefixes})\.[A-Za-z0-9_.]*[A-Za-z0-9_])["']""")
    sources = [*PKG.rglob("*.py"), PKG / "ui" / "app.js", PKG / "ui" / "index.html"]
    used = set()
    for path in sources:
        used |= set(pattern.findall(path.read_text(encoding="utf-8")))
    assert len(used) > 80  # the scan found the keys at all
    missing = sorted(k for k in used if k not in EN)
    assert not missing, missing


def test_dynamic_key_families_exist():
    rules_src = (PKG / "rules.py").read_text(encoding="utf-8")
    names = set(re.findall(r'_finding\("\w+",\s*"\w+",\s*"(\w+)"', rules_src)) | {"lean", "rich"}
    keys = [f"finding.{n}.title" for n in names] + [f"finding.{n}.detail" for n in names]
    keys += [f"ecu.{e.kind}" for e in KNOWN_ECUS] + ["ecu.unknown"]
    keys += [f"ident.{v}" for v in UDS_IDENT_DIDS.values()] + ["ident.kwp_ident"]
    keys += [f"ident.{v}" for v in RENAULT_IDENT_FIELDS]
    keys += [f"pid.{p:02X}" for p, d in obd2.PIDS.items() if d.label is None]
    keys += [f"pidnote.{d.note_id}" for d in obd2.PIDS.values() if d.note_id]
    keys += [f"mid.{m:02X}" for m in obd2.MODE06_MIDS]
    keys += [f"monitor.{m}" for m in obd2._SPARK_MONITORS + obd2._DIESEL_MONITORS if m]
    keys += [f"state.{s}" for s in ("active", "stored", "pending", "failed_earlier", "recorded")]
    keys += [f"severity.{s}" for s in ("critical", "high", "medium", "low", "info")]
    keys += [f"system_letter.{c}" for c in "PCBU"]
    keys += [f"report.{k}.{s}" for k in ("status", "status_label") for s in ("good", "attention", "problems", "critical")]
    missing = sorted(k for k in keys if k not in EN)
    assert not missing, missing


def test_formatting_and_fallbacks():
    m = i18n.msg("finding.lean.title", total=18.4)
    assert i18n.text(m, "en") == "Engine running lean: total fuel trim +18%"
    assert i18n.text(m, "es") == "Motor trabajando pobre: corrección total de combustible +18%"
    assert i18n.text([i18n.msg("monitor.o2"), i18n.msg("monitor.catalyst")], "es") == "Sensor de oxígeno, Catalizador"
    assert i18n.t("no.such.key", "es") == "no.such.key"
    assert i18n.normalize("Spanish") == "es" and i18n.normalize("xx") == "en"
    err = i18n.UserError("error.adapter_timeout", seconds=2)
    assert str(err) == "The adapter did not answer within 2.0 s."
    assert err.text("es") == "El adaptador no respondió en 2,0 s."   # Spanish decimal comma
    # thousands too: 89.420 km in Spanish is not 89 km
    assert i18n.t("finding.history_km", "es", km=89420) == "a los 89.420 km"
    assert i18n.t("finding.history_km", "en", km=89420) == "at 89,420 km"


def test_old_language_setting_is_migrated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text('{"language": "Spanish", "api_key": ""}', encoding="utf-8")
    assert settings.load()["language"] == "es"
