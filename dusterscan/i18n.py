"""Translations.

All user-visible text lives in dusterscan/locales/<code>.json, one file per
language. To add a language: copy en.json to <code>.json, translate the
values (keep the {placeholders}), and add the code to LANGUAGES below.

The scan report never stores finished sentences. It stores messages as
``{"key": ..., "params": {...}}`` (see ``msg``), which are turned into text in
the chosen language when the report is displayed. That way a saved scan can
be shown in any language.
"""

from __future__ import annotations

import ctypes
import json
import locale
import sys
from pathlib import Path

LANGUAGES = {"en": "English", "es": "Español"}
DEFAULT = "en"

_cache: dict[str, dict[str, str]] = {}


def _locale_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "dusterscan" / "locales"


def normalize(lang: str | None) -> str:
    """Accept a language code or an old-style name ("English", "Spanish")."""
    lang = (lang or "").strip().lower()
    aliases = {"english": "en", "spanish": "es", "español": "es", "espanol": "es"}
    lang = aliases.get(lang, lang[:2])
    return lang if lang in LANGUAGES else DEFAULT


def strings(lang: str) -> dict[str, str]:
    """All strings for a language, with English filling any gaps."""
    lang = normalize(lang)
    if lang not in _cache:
        data = json.loads((_locale_dir() / f"{lang}.json").read_text(encoding="utf-8"))
        if lang != DEFAULT:
            data = {**strings(DEFAULT), **data}
        _cache[lang] = data
    return _cache[lang]


def has(key: str) -> bool:
    return key in strings(DEFAULT)


def msg(key: str, **params) -> dict:
    """A translatable message, stored as data until it is displayed."""
    return {"key": key, "params": params}


def is_msg(value) -> bool:
    return isinstance(value, dict) and set(value) == {"key", "params"}


def _param(value, lang: str):
    if is_msg(value):
        return t(value["key"], lang, **value["params"])
    if isinstance(value, list):
        return ", ".join(str(_param(v, lang)) for v in value)
    return value  # numbers stay numbers so "{v:.1f}" works


def t(key: str, lang: str = DEFAULT, **params) -> str:
    template = strings(lang).get(key)
    if template is None:
        return key
    if not params:
        return template
    try:
        return template.format(**{k: _param(v, lang) for k, v in params.items()})
    except (KeyError, IndexError, ValueError):
        return template


def text(value, lang: str = DEFAULT) -> str:
    """Display any report value: a message, a list of messages, a number or plain text."""
    if value is None:
        return ""
    return str(_param(value, lang))


def resolve(obj, lang: str = DEFAULT):
    """Copy of a report structure with every message replaced by its text."""
    if is_msg(obj):
        return text(obj, lang)
    if isinstance(obj, dict):
        return {k: resolve(v, lang) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve(v, lang) for v in obj]
    return obj


def system_language() -> str:
    """The Windows display language, if we have a translation for it."""
    try:
        if sys.platform == "win32":
            primary = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
            return {0x09: "en", 0x0A: "es"}.get(primary, DEFAULT)
        return normalize((locale.getlocale()[0] or "")[:2])
    except Exception:
        return DEFAULT


class UserError(Exception):
    """An error whose message is shown to the user, in their language."""

    def __init__(self, key: str, **params):
        self.key, self.params = key, params
        super().__init__(t(key, DEFAULT, **params))

    def text(self, lang: str) -> str:
        return t(self.key, lang, **self.params)

    def as_msg(self) -> dict:
        return msg(self.key, **self.params)
