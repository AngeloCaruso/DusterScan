"""Which Renault definition belongs to which module.

A module identifies itself with its diagnostic version, supplier, software and
edition (the "21 80" block). DDT4All lists the same four values for each
definition. Tiers, best first:

  exact          all four match
  same_software  diagnostic version, supplier and software match (only the edition differs)
  same_family    diagnostic version and supplier match
  same_address   only the CAN address matches

Only exact and same_software are used automatically. Anything weaker keeps the
module generic, unless the user picks a definition from the list themselves.
"""

from __future__ import annotations

import re
from collections import Counter

from .store import Database

TIERS = ("exact", "same_software", "same_family", "same_address")
AUTO_TIERS = ("exact", "same_software")
LIST_LIMIT = 200


def module_ident(ecu: dict) -> dict | None:
    """The four matching values of a scanned module, or None if it did not give them."""
    ident = ecu.get("identification") or {}
    keys = ("diag_version", "supplier", "software", "edition")
    if not all(ident.get(k) for k in keys):
        return None
    return {"diag": int(ident["diag_version"]), "supplier": str(ident["supplier"]).strip().upper(),
            "soft": str(ident["software"]).upper(), "version": str(ident["edition"]).upper()}


def module_key(ecu: dict) -> str:
    """Remembers the user's choice per module *and* identification: a replaced module starts fresh."""
    i = module_ident(ecu)
    return f"{ecu['tx']}|{i['diag']}|{i['supplier']}|{i['soft']}|{i['version']}" if i else f"{ecu['tx']}|-"


def _diag_equal(raw: str, ours: int) -> bool:
    # The database writes the diagnostic version in decimal for some files ("68")
    # and in hex for others ("13" for 0x13).
    try:
        return int(raw) == ours or int(raw, 16) == ours
    except ValueError:
        return False


def tier(entry: dict, ident: dict | None) -> str:
    best = "same_address"
    if not ident:
        return best
    for i in entry["idents"]:
        if not (_diag_equal(i["diag"], ident["diag"]) and i["supplier"] == ident["supplier"]):
            continue
        if i["soft"].zfill(4) == ident["soft"].zfill(4):
            if i["version"].zfill(4) == ident["version"].zfill(4):
                return "exact"
            best = "same_software"
        elif best == "same_address":
            best = "same_family"
    return best


def file_date(name: str) -> str:
    """Definitions carry their release date in the name: ..._20200116T154114.json."""
    m = re.search(r"_(\d{8})T\d{6}", name)
    return m.group(1) if m else ""


def _at_address(db: Database, ecu: dict):
    want = (ecu["tx"].upper(), ecu["rx"].upper())
    for name, e in db.entries.items():
        if db.can_ids(e["address"]) == want:
            yield name, e


def _sort_key(item: tuple[str, dict, str]):
    name, _, t = item
    return (TIERS.index(t), "".join(chr(255 - ord(c)) for c in file_date(name)), name.lower())


def ranked(db: Database, ecu: dict) -> list[tuple[str, dict, str]]:
    """Every definition for this module's CAN address, best match first (newest first within a tier)."""
    ident = module_ident(ecu)
    return sorted(((n, e, tier(e, ident)) for n, e in _at_address(db, ecu)), key=_sort_key)


def auto_choice(db: Database, ecu: dict) -> tuple[str, str] | None:
    """(file, tier) when a definition matches well enough to use without asking, else None."""
    for name, _, t in ranked(db, ecu):
        if t in AUTO_TIERS:
            return name, t
        break
    return None


def vehicle_project(db: Database, ecus: list[dict]) -> str | None:
    """The vehicle project most exact matches share (x79Ph2 for a 2014+ Duster), for list filtering."""
    counts: Counter = Counter()
    for ecu in ecus:
        choice = auto_choice(db, ecu) if ecu.get("responded") else None
        if choice and choice[1] == "exact":
            counts.update(db.entries[choice[0]]["projects"])
    return counts.most_common(1)[0][0] if counts else None


def candidates(db: Database, ecu: dict, query: str = "", all_vehicles: bool = False,
               project: str | None = None, limit: int = LIST_LIMIT) -> dict:
    """The list behind the module's dropdown."""
    words = [w for w in query.lower().split() if w]
    items = []
    for name, e, t in ranked(db, ecu):
        if not all_vehicles and project and t == "same_address" and project not in e["projects"]:
            continue
        hay = f"{name} {e['ecuname']}".lower()
        if words and not all(w in hay for w in words):
            continue
        items.append({"file": name, "ecuname": e["ecuname"], "tier": t, "date": file_date(name)})
    return {"items": items[:limit], "more": max(0, len(items) - limit)}


def resolve(db: Database, ecu: dict, choices: dict) -> dict | None:
    """The definition to use for a module: the user's saved pick, else an automatic match, else None."""
    saved = choices.get(module_key(ecu))
    if saved == "generic":
        return None
    if saved and saved in db.entries:
        return {"file": saved, "tier": tier(db.entries[saved], module_ident(ecu)), "chosen": "user"}
    auto = auto_choice(db, ecu)
    return {"file": auto[0], "tier": auto[1], "chosen": "auto"} if auto else None
