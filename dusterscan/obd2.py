"""Standard OBD-II (SAE J1979) decoding: live data, DTCs, readiness, vehicle info."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .i18n import msg


@dataclass(frozen=True)
class Pid:
    pid: int
    unit: str
    nbytes: int
    decode: Callable[[bytes], float | dict]
    # Typical range for a warm engine at idle (None = no simple range).
    normal: tuple[float, float] | None = None
    note_id: str | None = None      # "pidnote.<id>" in the locale files
    label: dict | None = None       # overrides the default "pid.<XX>" name

    @property
    def name(self) -> dict:
        return self.label or msg(f"pid.{self.pid:02X}")

    @property
    def note(self) -> dict | None:
        return msg(f"pidnote.{self.note_id}") if self.note_id else None


def _u8(b): return b[0]
def _u16(b): return (b[0] << 8) | b[1]
def _pct(b): return round(b[0] * 100 / 255, 1)
def _temp(b): return b[0] - 40
def _trim(b): return round((b[0] - 128) * 100 / 128, 1)
def _o2v(b): return round(b[0] / 200, 3)


def _enum(prefix: str, known: set[int], fallback: str):
    """Decoder for one-byte coded values, returning a translatable message."""
    def decode(b: bytes) -> dict:
        if b[0] in known:
            return msg(f"{prefix}.{b[0]}")
        return msg(f"{prefix}.{fallback}", code=f"0x{b[0]:02X}")
    return decode


_fuel_status = _enum("fuel_status", {1, 2, 4, 8, 16}, "unknown")
_fuel_type = _enum("fuel_type", {1, 2, 3, 4, 5, 6, 8, 9, 12, 13}, "other")
_obd_std = _enum("obd_std", {1, 2, 3, 4, 5, 6, 7, 9, 10, 13, 17, 18}, "other")

CLOSED_LOOP = ({"key": "fuel_status.2", "params": {}}, {"key": "fuel_status.16", "params": {}})


PIDS: dict[int, Pid] = {p.pid: p for p in [
    Pid(0x03, "", 2, _fuel_status),
    Pid(0x04, "%", 1, _pct, (15, 40)),
    Pid(0x05, "°C", 1, _temp, (80, 105)),
    Pid(0x06, "%", 1, _trim, (-10, 10)),
    Pid(0x07, "%", 1, _trim, (-10, 10)),
    Pid(0x08, "%", 1, _trim, (-10, 10)),
    Pid(0x09, "%", 1, _trim, (-10, 10)),
    Pid(0x0A, "kPa", 1, lambda b: b[0] * 3),
    Pid(0x0B, "kPa", 1, _u8, (25, 45), "idle_warm"),
    Pid(0x0C, "rpm", 2, lambda b: round(_u16(b) / 4), (650, 950), "idle"),
    Pid(0x0D, "km/h", 1, _u8),
    Pid(0x0E, "°", 1, lambda b: b[0] / 2 - 64, (0, 20), "idle"),
    Pid(0x0F, "°C", 1, _temp, (-10, 60)),
    Pid(0x10, "g/s", 2, lambda b: round(_u16(b) / 100, 2), (2, 5), "maf"),
    Pid(0x11, "%", 1, _pct),
    Pid(0x1C, "", 1, _obd_std),
    Pid(0x1F, "s", 2, _u16),
    Pid(0x21, "km", 2, _u16),
    Pid(0x2C, "%", 1, _pct),
    Pid(0x2E, "%", 1, _pct),
    Pid(0x2F, "%", 1, _pct),
    Pid(0x30, "", 1, _u8),
    Pid(0x31, "km", 2, _u16),
    Pid(0x33, "kPa", 1, _u8, (70, 105), "baro"),
    Pid(0x3C, "°C", 2, lambda b: round(_u16(b) / 10 - 40, 1)),
    Pid(0x42, "V", 2, lambda b: round(_u16(b) / 1000, 2), (13.2, 14.8), "running"),
    Pid(0x43, "%", 2, lambda b: round(_u16(b) * 100 / 255, 1)),
    Pid(0x44, "λ", 2, lambda b: round(_u16(b) * 2 / 65536, 3)),
    Pid(0x45, "%", 1, _pct),
    Pid(0x46, "°C", 1, _temp),
    Pid(0x47, "%", 1, _pct),
    Pid(0x49, "%", 1, _pct),
    Pid(0x4A, "%", 1, _pct),
    Pid(0x4C, "%", 1, _pct),
    Pid(0x4D, "min", 2, _u16),
    Pid(0x4E, "min", 2, _u16),
    Pid(0x51, "", 1, _fuel_type),
    Pid(0x5C, "°C", 1, _temp, (80, 120)),
    Pid(0x5E, "L/h", 2, lambda b: round(_u16(b) / 20, 2)),
]}

# Narrowband O2 sensors: PIDs 0x14-0x1B = bank1 s1-s4, bank2 s1-s4 (voltage + trim).
for _i, _pid in enumerate(range(0x14, 0x1C)):
    _bank, _sensor = _i // 4 + 1, _i % 4 + 1
    PIDS[_pid] = Pid(_pid, "V", 2, _o2v, (0.1, 0.9), "o2_up" if _sensor == 1 else "o2_down",
                     label=msg("pid.o2", bank=_bank, sensor=_sensor))

# PIDs that are sampled repeatedly to see how they move, not just a single value.
SAMPLED_PIDS = [0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0B, 0x0C, 0x0E, 0x0F, 0x10, 0x11,
                0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x42, 0x44, 0x5C]


def decode_pid(pid: int, data: bytes) -> float | dict | None:
    p = PIDS.get(pid)
    if not p or len(data) < p.nbytes:
        return None
    return p.decode(data[:p.nbytes])


def parse_supported(bitmap_msgs: dict[int, bytes]) -> set[int]:
    """bitmap_msgs maps the base PID (0x00, 0x20, ...) to its 4 data bytes."""
    supported: set[int] = set()
    for base, data in bitmap_msgs.items():
        bits = int.from_bytes(data[:4], "big")
        for i in range(32):
            if bits & (1 << (31 - i)):
                supported.add(base + i + 1)
    return supported


# -- DTCs -------------------------------------------------------------------

def dtc_from_bytes(hi: int, lo: int) -> str:
    """Two bytes -> 'P0301' style code (SAE J2012 encoding)."""
    letter = "PCBU"[hi >> 6]
    return f"{letter}{(hi >> 4) & 0x3}{hi & 0xF:X}{lo >> 4:X}{lo & 0xF:X}"


def parse_obd_dtcs(data: bytes, can: bool) -> list[str]:
    """Parse a mode 03/07/0A response payload (starting at the response SID byte)."""
    body = data[1:]
    if can and body:
        body = body[1:]  # CAN responses carry a DTC count byte first
    codes = []
    for i in range(0, len(body) - 1, 2):
        hi, lo = body[i], body[i + 1]
        if hi == 0 and lo == 0:
            continue
        codes.append(dtc_from_bytes(hi, lo))
    return codes


# -- Readiness (PID 01 01) ----------------------------------------------------

_SPARK_MONITORS = ["catalyst", "heated_catalyst", "evap", "secondary_air", "ac", "o2", "o2_heater", "egr"]
_DIESEL_MONITORS = ["nmhc", "nox", None, "boost", None, "exhaust_sensor", "pm_filter", "egr_vvt"]


def _monitor(mid: str, supported: bool, complete: bool) -> dict:
    return {"id": mid, "name": msg(f"monitor.{mid}"), "supported": supported, "complete": complete}


def parse_monitor_status(data: bytes) -> dict:
    """Decode PID 01 01 (4 bytes A B C D)."""
    a, b, c, d = data[:4]
    diesel = bool(b & 0x08)
    monitors = [
        _monitor("misfire", bool(b & 0x01), not (b & 0x10)),
        _monitor("fuel_system", bool(b & 0x02), not (b & 0x20)),
        _monitor("components", bool(b & 0x04), not (b & 0x40)),
    ]
    ids = _DIESEL_MONITORS if diesel else _SPARK_MONITORS
    for bit, mid in enumerate(ids):
        if mid is not None:
            monitors.append(_monitor(mid, bool(c & (1 << bit)), not (d & (1 << bit))))
    return {
        "mil_on": bool(a & 0x80),
        "dtc_count": a & 0x7F,
        "ignition": msg("ignition.compression" if diesel else "ignition.spark"),
        "monitors": monitors,
    }


# -- Mode 09 ----------------------------------------------------------------

def parse_vin(data: bytes) -> str | None:
    """Mode 09 PID 02 response payload: 49 02 [count] VIN..."""
    if len(data) < 3:
        return None
    raw = data[3:] if len(data) >= 20 else data[2:]
    vin = bytes(c for c in raw if 0x30 <= c <= 0x5A).decode("ascii", "ignore")
    return vin[-17:] if len(vin) >= 17 else (vin or None)


def parse_text_items(data: bytes, item_len: int = 16) -> list[str]:
    """Mode 09 calibration IDs / ECU name: 49 xx [count] then fixed-width ASCII items."""
    body = data[3:]
    items = []
    for i in range(0, len(body), item_len):
        s = bytes(c for c in body[i:i + item_len] if 0x20 <= c < 0x7F).decode().strip()
        if s:
            items.append(s)
    return items


# -- Mode 06 (on-board monitor test results, CAN only) ------------------------

MODE06_MIDS = {0x01, 0x02, 0x03, 0x05, 0x06, 0x21, 0x22, 0x31, 0x35, 0x39, 0x3A, 0x3B, 0x3C,
               0x3D, 0x41, 0x42, 0x45, 0x61, 0x71, 0x81, 0x82, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5,
               0xA6, 0xA7}  # names: "mid.<XX>" in the locale files


def parse_mode06(data: bytes) -> list[dict]:
    """Mode 06 CAN response payload: 46 MID [TID UASID V(2) MIN(2) MAX(2)]*"""
    if len(data) < 2:
        return []
    mid = data[1]
    out = []
    body = data[2:]
    for i in range(0, len(body) - 8, 9):
        tid, uas = body[i], body[i + 1]
        signed = uas >= 0x80
        def num(hi, lo):
            v = (hi << 8) | lo
            return v - 0x10000 if signed and v & 0x8000 else v
        val, lo_lim, hi_lim = num(*body[i + 2:i + 4]), num(*body[i + 4:i + 6]), num(*body[i + 6:i + 8])
        passed = lo_lim <= val <= hi_lim
        span = hi_lim - lo_lim
        margin = None if span <= 0 else round(min(val - lo_lim, hi_lim - val) * 100 / span, 1)
        out.append({
            "mid": f"{mid:02X}",
            "monitor": msg(f"mid.{mid:02X}") if mid in MODE06_MIDS else msg("mid.unknown", mid=f"{mid:02X}"),
            "tid": f"{tid:02X}", "unit_scaling_id": f"{uas:02X}",
            "value_raw": val, "min_raw": lo_lim, "max_raw": hi_lim,
            "passed": passed, "margin_pct": margin,
        })
    return out
