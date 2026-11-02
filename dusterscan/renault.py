"""Read-only scan of the individual ECUs (controllers) of a Renault/Dacia vehicle.

Standard OBD-II only reaches the engine computer. To see ABS, airbag, body
control, instrument cluster, etc. we address each ECU directly on the CAN bus
and use the read services of UDS (newer ECUs) or KWP2000 (older Renault ECUs):
identification and fault codes. Nothing else.

Renault diagnostic CAN pairs follow a convention: the ECU answers on
(request ID + 0x20), e.g. ABS 740 -> 760. Engine and automatic gearbox use
the OBD-II IDs (7E0 -> 7E8, 7E1 -> 7E9).

What a 2018 Duster actually does (from real scans): most modules identify
themselves with the KWP2000 request 21 80 but report fault codes with the
UDS request 19 02; the automatic gearbox only answers in its extended session;
the engine and airbag computers take longer than 150 ms to answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from . import dtc as dtcinfo
from .elm327 import ELM327, AdapterError, Message
from .i18n import has, msg
from .obd2 import dtc_from_bytes


@dataclass(frozen=True)
class EcuAddress:
    kind: str   # "ecu.<kind>" in the locale files
    tx: int
    rx: int

    @property
    def name(self) -> dict:
        if self.kind == "unknown":
            return msg("ecu.unknown", id=f"{self.tx:03X}")
        return msg(f"ecu.{self.kind}")


# ECUs present on B0-platform vehicles (Duster, Logan, Sandero) depending on
# trim. An ECU that is not fitted simply does not answer.
KNOWN_ECUS = [
    EcuAddress("engine", 0x7E0, 0x7E8),
    EcuAddress("gearbox", 0x7E1, 0x7E9),
    EcuAddress("abs", 0x740, 0x760),
    EcuAddress("steering", 0x742, 0x762),
    EcuAddress("cluster", 0x743, 0x763),
    EcuAddress("climate", 0x744, 0x764),
    EcuAddress("body", 0x745, 0x765),
    EcuAddress("airbag", 0x752, 0x772),
]


def sweep_addresses() -> list[EcuAddress]:
    """Every plausible Renault diagnostic pair, for the deep scan."""
    known = {(e.tx, e.rx) for e in KNOWN_ECUS}
    out = [EcuAddress("unknown", tx, tx + 0x20) for tx in range(0x700, 0x7C0)]
    out += [EcuAddress("unknown", tx, tx + 8) for tx in range(0x7E0, 0x7E8)]
    return [e for e in out if (e.tx, e.rx) not in known]


# UDS identification DIDs -> "ident.<id>" labels
UDS_IDENT_DIDS = {
    0xF187: "spare_part",
    0xF18A: "supplier",
    0xF191: "hardware",
    0xF194: "sw_number",
    0xF195: "sw_version",
    0xF197: "system_name",
    0xF190: "vin",
}

# Fields of the Renault "21 80" identification block shown in the report
# ("ident.<id>" labels). The block also holds a diagnostic version, a
# calibration number and index bytes, kept only in the raw hex.
RENAULT_IDENT_FIELDS = ("part_suffix", "supplier", "hw_suffix", "software", "edition")

# UDS DTC status bits that mean the test actually failed at some point:
# failed now (0x01), failed this cycle (0x02), pending (0x04), confirmed (0x08),
# failed since last clear (0x20), warning lamp (0x80). Codes with only the
# "test not completed" bits (0x10, 0x40) are tests the ECU knows about but
# has not run or never failed; a 2018 Duster lists dozens of them.
UDS_FAULT_MASK = 0xAF

# Negative answers that mean "ask me again in another diagnostic session"
_WRONG_SESSION = {0x7E, 0x7F, 0x80, 0x22}

_VIN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


@dataclass
class EcuResult:
    kind: str
    name: dict
    tx: str
    rx: str
    responded: bool = False
    protocol: str = ""
    identification: dict = field(default_factory=dict)
    dtcs: list[dict] = field(default_factory=list)
    dtc_read_ok: bool = False
    notes: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _ascii_runs(data: bytes, min_len: int = 4) -> list[str]:
    text = "".join(chr(c) if 0x20 <= c < 0x7F else "\x00" for c in data)
    return [r.strip() for r in text.split("\x00") if len(r.strip()) >= min_len]


def parse_renault_ident(data: bytes) -> dict:
    """Decode a "61 80 ..." answer: Renault's fixed identification layout.

    After 61 80: part number (last 5 chars), diagnostic version, supplier code
    (3 chars), hardware number (last 5 chars), software (2 bytes), edition
    (2 bytes), calibration (2 bytes), 3 index bytes, manufacturer code 0x88.
    """
    b = data[2:]

    def ascii_field(chunk: bytes) -> str | None:
        return chunk.decode("ascii") if all(0x20 <= c < 0x7F for c in chunk) else None

    if len(b) >= 18:
        part, supplier, hw = ascii_field(b[0:5]), ascii_field(b[6:9]), ascii_field(b[9:14])
        if part and supplier and hw:
            return {"part_suffix": part.strip(), "supplier": supplier.strip(), "hw_suffix": hw.strip(),
                    "software": b[14:16].hex().upper(), "edition": b[16:18].hex().upper()}
    return {"kwp_ident": " | ".join(_ascii_runs(b)) or "-"}


def valid_vin(value) -> str | None:
    """The VIN if it looks like a real one (unprogrammed modules return blanks or FF)."""
    if not isinstance(value, str):
        return None
    v = value.strip().upper()
    return v if _VIN.match(v) else None


def _first(msgs: list[Message], positive_sid: int) -> Message | None:
    for m in msgs:
        if m.data and m.data[0] == positive_sid:
            return m
    return None


def _negative(msgs: list[Message]) -> int | None:
    for m in msgs:
        if m.is_negative:
            return m.nrc
    return None


def _refusal_note(proto: str, request: str, nrc: int | None) -> dict:
    if nrc is None:
        return msg("note.dtc_no_answer", proto=proto, request=request)
    key = f"nrc.{nrc:02X}"
    reason = msg(key) if has(key) else msg("nrc.other", code=f"0x{nrc:02X}")
    return msg("note.dtc_refused", proto=proto, request=request, reason=reason)


class EcuScanner:
    def __init__(self, elm: ELM327, log: Callable[[dict], None] = lambda m: None):
        self.elm = elm
        self.log = log

    def probe(self, addr: EcuAddress, fast: bool = False) -> EcuResult:
        res = EcuResult(addr.kind, addr.name, f"{addr.tx:03X}", f"{addr.rx:03X}")
        self.elm.set_target(addr.tx, addr.rx)
        try:
            kwp = self.elm.request("2180", timeout=1.5)
            if not fast and _negative(kwp) in _WRONG_SESSION:
                kwp = self._in_session("2180", "10C0", "1081")
        except AdapterError as e:
            res.notes.append(e.as_msg())
            return res
        if kwp:
            res.responded = True
            pos = _first(kwp, 0x61)
            if pos:
                res.protocol = "KWP2000"
                res.identification.update(parse_renault_ident(pos.data))
                res.identification["raw"] = pos.hex()
            else:
                res.protocol = "UDS"
        elif not fast:
            uds = self.elm.request("22F190", timeout=1.5)
            if uds:
                res.responded = True
                res.protocol = "UDS"
        return res

    def read_details(self, res: EcuResult) -> None:
        tx, rx = int(res.tx, 16), int(res.rx, 16)
        self.elm.set_target(tx, rx)
        if res.protocol == "UDS":
            self._uds_ident(res)
        else:
            self._kwp_vin(res)
        # Renault modules that identify over KWP2000 mostly report fault codes
        # over UDS, so UDS goes first for everyone. Refusals only matter (and
        # are only kept) when no method worked.
        attempts: list[dict] = []
        if not self._read_dtcs_uds(res, attempts):
            self._read_dtcs_kwp(res, attempts)
        if not res.dtc_read_ok:
            res.notes += attempts

    # -- identification ------------------------------------------------------

    def _uds_ident(self, res: EcuResult) -> None:
        for did, label in UDS_IDENT_DIDS.items():
            try:
                msgs = self.elm.request(f"22{did:04X}", timeout=1.5)
            except AdapterError:
                continue
            pos = _first(msgs, 0x62)
            if pos and len(pos.data) > 3:
                runs = _ascii_runs(pos.data[3:], 2)
                res.identification[label] = " ".join(runs) if runs else pos.data[3:].hex(" ").upper()

    def _kwp_vin(self, res: EcuResult) -> None:
        """Renault KWP modules keep the VIN they were coded with at local identifier 0x81."""
        try:
            pos = _first(self.elm.request("2181", timeout=1.5), 0x61)
        except AdapterError:
            return
        if pos and len(pos.data) >= 19 and pos.data[1] == 0x81:
            vin = valid_vin(pos.data[2:19].decode("ascii", "replace"))
            if vin:
                res.identification["vin"] = vin

    # -- fault codes -----------------------------------------------------------

    def _in_session(self, payload: str, session: str, default: str) -> list[Message]:
        # Some ECUs only answer in their extended diagnostic session. Entering it
        # does not change anything in the ECU and it times out on its own;
        # we still switch back to the default session right after.
        self.log(msg("log.extended_session", request=payload))
        self.elm.request(session, timeout=1.5)
        try:
            return self.elm.request(payload, timeout=3.0)
        finally:
            self.elm.request(default, timeout=1.5)

    def _with_session_retry(self, payload: str, positive: int, session: str, default: str):
        msgs = self.elm.request(payload, timeout=3.0)
        pos = _first(msgs, positive)
        nrc = _negative(msgs)
        if pos or (msgs and nrc not in _WRONG_SESSION):
            return msgs, pos
        msgs = self._in_session(payload, session, default)
        return msgs, _first(msgs, positive)

    def _read_dtcs_uds(self, res: EcuResult, attempts: list[dict]) -> bool:
        pos = None
        for payload in ("1902AF", "1902FF"):  # FF only if an ECU refuses the fault-bit mask
            try:
                msgs, pos = self._with_session_retry(payload, 0x59, "1003", "1001")
            except AdapterError as e:
                attempts.append(msg("note.dtc_read_failed", proto="UDS", error=e.as_msg()))
                return False
            if pos:
                break
            attempts.append(_refusal_note("UDS", payload, _negative(msgs)))
            if _negative(msgs) not in (0x12, 0x31):
                return False
        if not pos:
            return False
        body = pos.data[3:]
        for i in range(0, len(body) - 3, 4):
            hi, mid, ftb, status = body[i:i + 4]
            if hi == mid == ftb == 0 or not status & UDS_FAULT_MASK:
                continue
            res.dtcs.append(_dtc_entry(hi, mid, status, ftb=ftb, uds=True))
        res.dtc_read_ok = True
        res.protocol = res.protocol or "UDS"
        return True

    def _read_dtcs_kwp(self, res: EcuResult, attempts: list[dict]) -> bool:
        for payload, positive in (("17FF00", 0x57), ("1800FF00", 0x58)):
            try:
                msgs, pos = self._with_session_retry(payload, positive, "10C0", "1081")
            except AdapterError as e:
                attempts.append(msg("note.dtc_read_failed", proto="KWP", error=e.as_msg()))
                continue
            if not pos:
                attempts.append(_refusal_note("KWP", payload, _negative(msgs)))
                continue
            count = pos.data[1] if len(pos.data) > 1 else 0
            body = pos.data[2:]
            for i in range(0, min(len(body), count * 3) - 2, 3):
                hi, lo, status = body[i:i + 3]
                if hi == lo == 0:
                    continue
                res.dtcs.append(_dtc_entry(hi, lo, status, uds=False))
            res.dtc_read_ok = True
            res.protocol = res.protocol or "KWP2000"
            return True
        return False


def _dtc_entry(hi: int, lo: int, status: int, ftb: int | None = None, uds: bool = True) -> dict:
    code = dtc_from_bytes(hi, lo)
    desc, generic = dtcinfo.describe(code)
    st = dtcinfo.describe_uds_status(status) if uds else dtcinfo.describe_kwp_status(status)
    state = dtcinfo.state_id(st)
    entry = {
        "code": code,
        "raw": f"{hi:02X}{lo:02X}" + (f"{ftb:02X}" if ftb is not None else ""),
        "description": desc,
        "generic": generic,
        "status_byte": f"{status:02X}",
        "status": st,
        "state_id": state,
        "state": msg(f"state.{state}"),
    }
    if ftb:  # 0x00 means "no sub-type information": nothing worth showing
        entry["failure_type"] = dtcinfo.failure_type(ftb)
    return entry
