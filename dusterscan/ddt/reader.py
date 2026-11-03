"""Reading a module with its Renault definition: fault-code details and values.

What may be sent, from a definition (everything else in it is ignored):
  * module values: requests of service 21 or 22 with no parameters, not marked
    for manual sending, not restricted to special sessions, at most 7 bytes;
  * fault-code details: the definition's own "19 06" records, with the code
    filled in.
Each of them still goes through safety.check_command before it is sent.

If a module's answers do not fit the definition (wrong reply layout, or it
refuses everything), the definition is dropped for that scan and the module is
reported generically, as without a database.
"""

from __future__ import annotations

import re
import time
from typing import Callable

from .. import safety
from ..elm327 import ELM327, Message
from ..i18n import msg
from ..renault import extended_session, session_requests, wrong_session
from ..safety import WriteBlockedError
from .decode import decode_request, unit_of
from .store import Database, repair

READ_SIDS = (0x21, 0x22)
STANDARD_BUDGET_S = 20.0   # per module in a standard scan; a deep scan reads everything
REJECT_AFTER = 8           # unusable answers in a row before a definition is dropped

# Fault-code records worth reading (others: emission counters, internal timers)
DTC_RECORDS = {"Mileage": "mileage_km", "DTCOccurrenceCounter": "occurrences",
               "AgingCounter": "aging", "DTCFirstAppearMileage": "first_mileage_km"}
NO_MILEAGE = 0xFFFFFF      # "not recorded"

_IGNORED = re.compile(r"not used|unused|reserved|spare|non utilis", re.I)
_SUPPLY = re.compile(r"supply|batter|alimentation|tension", re.I)
_ODOMETER = re.compile(r"total.?mileage|odometer|odom|kilom", re.I)


def _sent(request: dict) -> bytes | None:
    try:
        return bytes.fromhex(str(request.get("sentbytes", "")))
    except ValueError:
        return None


def _allowed(sentbytes: str) -> bool:
    try:
        safety.check_command(sentbytes)
        return True
    except WriteBlockedError:
        return False


def is_readable(request: dict) -> bool:
    sent = _sent(request)
    if not sent or sent[0] not in READ_SIDS or len(sent) > safety.MAX_REQUEST_BYTES:
        return False
    if request.get("manualsend") or request.get("sendbyte_dataitems") or request.get("deny_sds"):
        return False
    return bool(request.get("receivebyte_dataitems")) and _allowed(request["sentbytes"])


def readable_requests(definition: dict) -> list[dict]:
    return [r for r in definition.get("requests", []) if is_readable(r)]


def dtc_record_requests(definition: dict) -> list[dict]:
    """The definition's "19 06 <code> <record>" requests for the records we use."""
    out = []
    for r in definition.get("requests", []):
        sent = _sent(r)
        if not sent or len(sent) != 6 or sent[:2] != b"\x19\x06" or sent[5] == 0xFF:
            continue
        if r.get("manualsend") or r.get("deny_sds"):
            continue
        params = r.get("sendbyte_dataitems") or {}
        if set(params) != {"DTCMaskRecord"} or int(params["DTCMaskRecord"].get("firstbyte", 0)) != 3:
            continue
        if any(i.split(".", 1)[-1] in DTC_RECORDS for i in r.get("receivebyte_dataitems", {})):
            out.append(r)
    return out


def request_tier(definition: dict, request: dict) -> int:
    """0 = measurements, 1 = states with named values, 2 = configuration / identification."""
    data = definition.get("data", {})
    best = 3
    for item in request.get("receivebyte_dataitems", {}):
        d = data.get(item) or {}
        if d.get("unit") or (d.get("scaled") and any(k in d for k in ("step", "offset", "divideby"))):
            best = min(best, 0)
        elif d.get("lists"):
            best = min(best, 1)
        else:
            best = min(best, 2)
    return best


def renault_names(definition: dict) -> dict[int, str]:
    """Fault code (2 bytes) -> Renault's name for it in this module."""
    out = {}
    for dev in definition.get("devices", []):
        try:
            code = int(dev.get("dtc", 0))
        except (TypeError, ValueError):
            continue
        if code and dev.get("name"):
            out[code] = repair(str(dev["name"])).strip()
    return out


def apply_names(ecu: dict, definition: dict) -> None:
    """Renault names for the module's codes. Codes the definition does not list keep the generic text."""
    names = renault_names(definition)
    for d in ecu.get("dtcs", []):
        try:
            code = int(str(d.get("raw", ""))[:4], 16)
        except ValueError:
            continue
        if code in names:
            d["renault_name"] = names[code]
        else:
            d.pop("renault_name", None)


def screen_map(layout: dict) -> tuple[dict, list[str]]:
    """(request, item) -> screen name, and the screens in the layout's category order."""
    screens = layout.get("screens") or {}
    order: list[str] = []
    for names in (layout.get("categories") or {}).values():
        order += [n for n in names if n in screens and n not in order]
    order += [n for n in screens if n not in order]
    where = {}
    for name in order:
        for disp in screens[name].get("displays", []):
            where.setdefault((disp.get("request"), disp.get("text")), repair(name))
    return where, [repair(n) for n in order]


class ModuleReader:
    def __init__(self, elm: ELM327, ecu: dict, db: Database, choice: dict,
                 log: Callable[[dict], None] = lambda m: None):
        self.elm, self.ecu, self.db, self.choice, self.log = elm, ecu, db, choice, log
        self.definition = db.definition(choice["file"]) or {}
        self.layout = db.layout(choice["file"]) or {}
        self.tx, self.rx = int(ecu["tx"], 16), int(ecu["rx"], 16)
        self.session = session_requests(ecu.get("protocol", ""))
        self.rejected: dict | None = None
        if not self.definition:
            self.rejected = msg("ddt.rejected_missing")
        elif db.definition_ids(choice["file"]) != (ecu["tx"].upper(), ecu["rx"].upper()):
            self.rejected = msg("ddt.rejected_address")

    # -- talking to the module --------------------------------------------------

    def _ask(self, payload: str) -> list[Message]:
        if not _allowed(payload):  # never happens for filtered requests; belt and braces
            return []
        self.elm.set_target(self.tx, self.rx)
        return self.elm.request(payload, timeout=3.0)

    @staticmethod
    def _positive(msgs: list[Message], sid: int) -> Message | None:
        return next((m for m in msgs if m.data and m.data[0] == sid + 0x40), None)

    # -- fault codes --------------------------------------------------------------

    def info(self) -> dict:
        return {"file": self.choice["file"], "ecuname": self.db.entries[self.choice["file"]]["ecuname"],
                "tier": self.choice["tier"], "chosen": self.choice["chosen"],
                "status": "rejected" if self.rejected else "used",
                **({"reason": self.rejected} if self.rejected else {})}

    def dtc_details(self) -> None:
        """Mileage when each code was stored, how often it happened, from the module's "19 06" records."""
        records = dtc_record_requests(self.definition)
        for d in self.ecu.get("dtcs", []):
            raw = str(d.get("raw", ""))
            if len(raw) != 6 or self.rejected:
                continue
            ext = {}
            for r in records:
                sent = bytearray(bytes.fromhex(r["sentbytes"]))
                sent[2:5] = bytes.fromhex(raw)
                pos = self._positive(self._ask(sent.hex().upper()), 0x19)
                if not pos or pos.data[2:5] != bytes(sent[2:5]):
                    continue
                for item, value in decode_request(self.definition, r, pos.data).items():
                    key = DTC_RECORDS.get(item.split(".", 1)[-1])
                    if key and key not in ext and isinstance(value, (int, float)) and value != NO_MILEAGE:
                        ext[key] = value
            if ext:
                d["extended"] = ext

    # -- values ---------------------------------------------------------------------

    def _ordered(self, only: set[str] | None = None) -> list[dict]:
        where, _ = screen_map(self.layout)
        shown = {req for req, _ in where}
        reqs = [r for r in readable_requests(self.definition) if only is None or r["name"] in only]
        return sorted(reqs, key=lambda r: (request_tier(self.definition, r), r["name"] not in shown))

    def read_values(self, budget_s: float | None = STANDARD_BUDGET_S, only: set[str] | None = None) -> dict:
        """Read the module's values; with a budget, the most informative ones first."""
        reqs = self._ordered(only)
        started = time.monotonic()
        values: dict[str, tuple[str, object]] = {}   # item -> (request, value)
        raw: dict[str, str] = {}
        retry: list[dict] = []
        good = bad_in_a_row = refused = asked = 0
        partial = False

        def take(r: dict, msgs: list[Message]) -> None:
            nonlocal good, bad_in_a_row, refused
            pos = self._positive(msgs, int(r["sentbytes"][:2], 16))
            decoded = decode_request(self.definition, r, pos.data) if pos else {}
            if decoded:
                good += 1
                bad_in_a_row = 0
                raw[r["name"]] = pos.hex()
                for item, v in decoded.items():
                    values.setdefault(item, (r["name"], v))
            elif pos or (msgs and not wrong_session(msgs)):
                bad_in_a_row += 1
                refused += 1
            if bad_in_a_row >= REJECT_AFTER or (good == 0 and refused >= REJECT_AFTER):
                self.rejected = msg("ddt.rejected_answers")

        for r in reqs:
            if self.rejected:
                break
            if budget_s is not None and time.monotonic() - started > budget_s:
                partial = True
                break
            msgs = self._ask(r["sentbytes"])
            asked += 1
            if wrong_session(msgs):
                retry.append(r)
                continue
            take(r, msgs)
        if retry and not self.rejected and not partial:
            self.log(msg("log.module_session", n=len(retry)))
            with extended_session(self.elm, *self.session):
                for r in retry:
                    if budget_s is not None and time.monotonic() - started > budget_s:
                        partial = True
                        break
                    take(r, self._ask(r["sentbytes"]))
        if self.rejected:
            return {}
        self.ecu["module_raw"] = raw
        return self._summary(values, asked, len(reqs), partial, time.monotonic() - started)

    def _summary(self, values: dict, asked: int, total: int, partial: bool, seconds: float) -> dict:
        where, order = screen_map(self.layout)
        other = msg("ddt.group_other")
        groups: dict[str, list] = {}
        supply = odometer = None
        for item, (req, v) in values.items():
            unit = unit_of(self.definition, item)
            if isinstance(v, (int, float)):
                if unit == "V" and _SUPPLY.search(item) and 6 <= v <= 18 and supply is None:
                    supply = v
                if unit == "km" and _ODOMETER.search(item) and 0 < v < 2_000_000 and odometer is None:
                    odometer = v
            if _IGNORED.search(item):
                continue
            groups.setdefault(where.get((req, item), ""), []).append(
                {"item": repair(item), "value": v, "unit": unit, "request": req})
        ordered = [{"name": g, "values": groups[g]} for g in order if g in groups]
        if "" in groups:
            ordered.append({"name": other, "values": groups[""]})
        out = {"groups": ordered, "read": asked, "total": total, "partial": partial,
               "seconds": round(seconds, 1)}
        if supply is not None:
            out["supply_voltage"] = supply
        if odometer is not None:
            out["odometer_km"] = odometer
        return out

    # -- live data ------------------------------------------------------------------

    def group_requests(self, group: str) -> list[dict]:
        """The readable requests behind one layout screen (for live data)."""
        where, _ = screen_map(self.layout)
        names = {req for (req, _), screen in where.items() if screen == group}
        return [r for r in self._ordered(names)]

    def read_once(self, request: dict) -> tuple[dict, bool]:
        """One request's values for live data: ({item: value}, refused because of the session)."""
        msgs = self._ask(request["sentbytes"])
        pos = self._positive(msgs, int(request["sentbytes"][:2], 16))
        decoded = decode_request(self.definition, request, pos.data) if pos else {}
        return {k: v for k, v in decoded.items() if not _IGNORED.search(k)}, wrong_session(msgs)

    def live_params(self, requests: list[dict]) -> list[dict]:
        """The values one group shows in live data, as tiles: id, name, unit."""
        out = []
        for r in requests:
            for item in r.get("receivebyte_dataitems", {}):
                if item in self.definition.get("data", {}) and not _IGNORED.search(item):
                    out.append({"pid": item, "name": repair(item), "unit": unit_of(self.definition, item),
                                "normal": None})
        return out


def strip(ecu: dict) -> None:
    """Back to generic: remove everything a definition added."""
    for d in ecu.get("dtcs", []):
        d.pop("renault_name", None)
        d.pop("extended", None)
    for key in ("module_data", "module_raw", "definition"):
        ecu.pop(key, None)


def enrich_module(elm: ELM327 | None, ecu: dict, db: Database, choice: dict,
                  budget_s: float | None = STANDARD_BUDGET_S, log: Callable[[dict], None] = lambda m: None) -> None:
    """Apply a definition to a scanned module. Without a connection (elm None) only the code names."""
    strip(ecu)
    reader = ModuleReader(elm, ecu, db, choice, log)
    if not reader.rejected:
        apply_names(ecu, reader.definition)
        if elm is not None:
            reader.dtc_details()
            data = reader.read_values(budget_s)
            if data:
                ecu["module_data"] = data
    if reader.rejected:
        strip(ecu)
    ecu["definition"] = reader.info()


def groups_of(db: Database, choice: dict) -> list[str]:
    """Layout screens that have at least one readable value (for the live data group list)."""
    definition = db.definition(choice["file"]) or {}
    where, order = screen_map(db.layout(choice["file"]) or {})
    readable = {r["name"] for r in readable_requests(definition)}
    present = {screen for (req, _), screen in where.items() if req in readable}
    return [g for g in order if g in present]
