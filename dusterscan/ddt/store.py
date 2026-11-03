"""The imported Renault module database (DDT4All's ecu.zip format).

The user imports their own copy once. It is copied to
%LOCALAPPDATA%\\DusterScan\\ddt\\ecu.zip together with a small index, and it is
never bundled with the app or sent anywhere. Definitions are read straight
from the zip when a module needs one.

Format, as found in DDT4All's ecu.zip:
  db.json             {file name: {address, group, protocol, projects, ecuname, autoidents}}
  <file>.json         one module definition: obd ids, requests, data items, fault-code names
  <file>.json.layout  the screens that group the values
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import zipfile
from functools import lru_cache
from pathlib import Path
from typing import Callable

from ..i18n import UserError

DDT_DIR = Path(os.environ.get("DUSTERSCAN_DDT_DIR")
               or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "DusterScan" / "ddt")
INDEX_VERSION = 1

ProgressFn = Callable[[float], None]


class DatabaseError(UserError):
    pass


def _zip_path() -> Path:
    return DDT_DIR / "database.zip"


def _index_path() -> Path:
    return DDT_DIR / "index.json"


# -- text ------------------------------------------------------------------

def decode_text(raw: bytes) -> str:
    """Definitions are UTF-8, layouts are sometimes Windows-1252."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")


def repair(s: str) -> str:
    """Undo double-encoded accents in database text ("Â°C" -> "°C", "GÃ©nÃ©rale" -> "Générale")."""
    if isinstance(s, str) and ("Ã" in s or "Â" in s):
        try:
            return s.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return s
    return s


# -- identification values ----------------------------------------------------

def _supplier(value) -> str:
    """Supplier codes appear both as text ("037") and as its hex ("39324d" = "92M")."""
    s = str(value or "").strip()
    if len(s) == 6 and re.fullmatch(r"[0-9A-Fa-f]{6}", s):
        try:
            text = bytes.fromhex(s).decode("ascii")
            if text.isprintable():
                return text.upper()
        except (ValueError, UnicodeDecodeError):
            pass
    return s.upper()


def _ident(ai: dict) -> dict:
    return {"diag": str(ai.get("diagnostic_version", ai.get("diagversion", ""))).strip().upper(),
            "supplier": _supplier(ai.get("supplier_code", ai.get("supplier", ""))),
            "soft": str(ai.get("soft_version", ai.get("soft", ""))).strip().upper(),
            "version": str(ai.get("version", "")).strip().upper()}


def _entries(db: dict) -> dict:
    out = {}
    for name, e in db.items():
        if not isinstance(e, dict) or not name.endswith(".json"):
            continue
        out[name] = {"address": str(e.get("address", "")).upper(),
                     "protocol": str(e.get("protocol", "")),
                     "projects": [str(p) for p in e.get("projects", [])],
                     "ecuname": repair(str(e.get("ecuname", name))),
                     "idents": [_ident(a) for a in e.get("autoidents", []) if isinstance(a, dict)]}
    return out


def _obd_ids(definition: dict) -> tuple[str, str] | None:
    obd = definition.get("obd") or {}
    tx, rx = str(obd.get("send_id", "")).upper(), str(obd.get("recv_id", "")).upper()
    return (tx, rx) if re.fullmatch(r"[0-9A-F]{3}", tx) and re.fullmatch(r"[0-9A-F]{3}", rx) else None


# -- database -----------------------------------------------------------------

class Database:
    """Read access to the definitions, from the imported zip or the built-in demo set."""

    def __init__(self, info: dict, entries: dict, can_map: dict, read: Callable[[str], bytes | None]):
        self.info = info
        self.entries = entries
        self.can_map = {a: tuple(ids) for a, ids in can_map.items()}
        self._read = read
        self._cache: dict[str, dict | None] = {}

    def can_ids(self, address: str) -> tuple[str, str] | None:
        return self.can_map.get(address.upper())

    def _load(self, member: str) -> dict | None:
        if member not in self._cache:
            raw = self._read(member)
            try:
                self._cache[member] = json.loads(decode_text(raw)) if raw is not None else None
            except ValueError:
                self._cache[member] = None
            if len(self._cache) > 24:  # keep memory small; definitions can be a few MB each
                self._cache.pop(next(iter(self._cache)))
        return self._cache[member]

    def definition(self, name: str) -> dict | None:
        return self._load(name) if name in self.entries else None

    def layout(self, name: str) -> dict | None:
        return self._load(name + ".layout") if name in self.entries else None

    def definition_ids(self, name: str) -> tuple[str, str] | None:
        d = self.definition(name)
        return _obd_ids(d) if d else None


def status() -> dict | None:
    """What is imported, for the settings dialog; None if nothing is."""
    try:
        idx = json.loads(_index_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if idx.get("version") != INDEX_VERSION or not _zip_path().exists():
        return None
    return {k: idx.get(k) for k in ("count", "imported_at", "source_name", "source_size")}


@lru_cache(maxsize=2)
def _load_imported(stamp: float) -> Database | None:
    idx = json.loads(_index_path().read_text(encoding="utf-8"))
    info = {k: idx.get(k) for k in ("count", "imported_at", "source_name", "source_size")}
    return Database(info, idx["entries"], idx["can_map"], _zip_reader(_zip_path()))


def load() -> Database | None:
    """The imported database, or None."""
    if not status():
        return None
    try:
        return _load_imported(_index_path().stat().st_mtime)
    except (OSError, ValueError, KeyError):
        return None


@lru_cache(maxsize=1)
def demo() -> Database:
    """Invented definitions for the simulated car (demo mode and tests); not Renault data."""
    from .demo import database

    db, contents = database()
    files = {name: json.dumps(body).encode() for name, body in contents.items()}
    entries = _entries(db)
    can_map = _can_map(entries, lambda n: json.loads(files[n]) if n in files else None, lambda n: 0)
    info = {"count": len(entries), "imported_at": None, "source_name": "demo", "source_size": 0, "demo": True}
    return Database(info, entries, can_map, files.get)


def write_demo_zip(path: str | Path) -> Path:
    """The demo definitions as a DDT4All-style ecu.zip (to test importing)."""
    from .demo import database

    db, contents = database()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("db.json", json.dumps(db))
        for name, body in contents.items():
            z.writestr(name, json.dumps(body))
    return Path(path)


def _can_map(entries: dict, open_def: Callable[[str], dict | None], size: Callable[[str], int]) -> dict:
    """DDT address -> CAN ids, from the smallest CAN definition at each address."""
    by_addr: dict[str, list[str]] = {}
    for name, e in entries.items():
        if e["protocol"].upper().startswith("CAN") and e["address"]:
            by_addr.setdefault(e["address"], []).append(name)
    out = {}
    for addr, names in by_addr.items():
        for name in sorted(names, key=size)[:3]:
            ids = _obd_ids(open_def(name) or {})
            if ids:
                out[addr] = list(ids)
                break
    return out


def _validate(src: Path) -> dict:
    """The entries of a DDT4All ecu.zip, or a DatabaseError saying why it is not one."""
    if not zipfile.is_zipfile(src):
        raise DatabaseError("error.ddt_not_zip")
    try:
        with zipfile.ZipFile(src) as z:
            db = json.loads(decode_text(z.read("db.json")))
    except (KeyError, ValueError, zipfile.BadZipFile):
        raise DatabaseError("error.ddt_not_ddt") from None
    entries = _entries(db) if isinstance(db, dict) else {}
    if not entries or not any(e["idents"] for e in entries.values()):
        raise DatabaseError("error.ddt_not_ddt")
    return entries


def _zip_can_map(path: Path, entries: dict) -> dict:
    with zipfile.ZipFile(path) as z:
        sizes = {i.filename: i.file_size for i in z.infolist()}

        def open_def(name: str) -> dict | None:
            try:
                return json.loads(decode_text(z.read(name)))
            except (KeyError, ValueError):
                return None
        return _can_map(entries, open_def, lambda n: sizes.get(n, 1 << 30))


def _zip_reader(path: Path) -> Callable[[str], bytes | None]:
    def read(member: str) -> bytes | None:
        with zipfile.ZipFile(path) as z:
            try:
                return z.read(member)
            except KeyError:
                return None
    return read


def open_zip(src: str | Path) -> Database:
    """A database straight from a DDT4All ecu.zip, without importing it (used by the tests)."""
    src = Path(src)
    entries = _validate(src)
    info = {"count": len(entries), "imported_at": None, "source_name": src.name, "source_size": src.stat().st_size}
    return Database(info, entries, _zip_can_map(src, entries), _zip_reader(src))


def import_zip(src: str | Path, progress: ProgressFn = lambda f: None) -> dict:
    """Validate a DDT4All ecu.zip, copy it into the app's folder and index it."""
    src = Path(src)
    entries = _validate(src)
    DDT_DIR.mkdir(parents=True, exist_ok=True)
    part = DDT_DIR / "database.zip.part"
    total = max(1, src.stat().st_size)
    with open(src, "rb") as fin, open(part, "wb") as fout:
        done = 0
        while chunk := fin.read(4 * 1024 * 1024):
            fout.write(chunk)
            done += len(chunk)
            progress(0.9 * done / total)
    can_map = _zip_can_map(part, entries)
    os.replace(part, _zip_path())
    index = {"version": INDEX_VERSION, "count": len(entries), "source_name": src.name,
             "source_size": src.stat().st_size,
             "imported_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
             "entries": entries, "can_map": can_map}
    _index_path().write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    _load_imported.cache_clear()
    progress(1.0)
    return status() or {}


def remove() -> None:
    for p in (_zip_path(), _index_path(), DDT_DIR / "database.zip.part"):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    _load_imported.cache_clear()
