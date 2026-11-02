"""ELM327 adapter driver (works with the Vgate vLinker FS and other ELM327 clones).

All traffic goes through ``ELM327._send``, which runs every command through
``safety.check_command`` first. There is no other code path that writes to
the serial port.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Protocol

from . import safety
from .i18n import UserError


class Transport(Protocol):
    """The subset of ``serial.Serial`` this driver uses (the simulator implements it too)."""

    @property
    def in_waiting(self) -> int: ...
    def write(self, data: bytes) -> int | None: ...
    def read(self, size: int = 1) -> bytes: ...
    def reset_input_buffer(self) -> None: ...
    def close(self) -> None: ...


class AdapterError(UserError):
    """The adapter or the vehicle bus reported an error."""


@dataclass
class Message:
    """One complete (reassembled) response from one ECU."""

    header: str | None
    data: bytes

    @property
    def is_negative(self) -> bool:
        return len(self.data) >= 3 and self.data[0] == 0x7F

    @property
    def nrc(self) -> int | None:
        return self.data[2] if self.is_negative else None

    def hex(self) -> str:
        return self.data.hex(" ").upper()


PROTOCOLS = {
    "1": "SAE J1850 PWM",
    "2": "SAE J1850 VPW",
    "3": "ISO 9141-2",
    "4": "ISO 14230-4 KWP (5 baud init)",
    "5": "ISO 14230-4 KWP (fast init)",
    "6": "ISO 15765-4 CAN (11 bit, 500 kbaud)",
    "7": "ISO 15765-4 CAN (29 bit, 500 kbaud)",
    "8": "ISO 15765-4 CAN (11 bit, 250 kbaud)",
    "9": "ISO 15765-4 CAN (29 bit, 250 kbaud)",
    "A": "SAE J1939 CAN",
}

_ERROR_MARKERS = (
    "UNABLE TO CONNECT", "CAN ERROR", "BUS ERROR", "BUS INIT", "FB ERROR",
    "DATA ERROR", "BUFFER FULL", "STOPPED", "LV RESET", "ERR",
)

_HEXLINE = re.compile(r"^[0-9A-F]{2}( [0-9A-F]{2})*$")


class ELM327:
    def __init__(self, transport: Transport, port_name: str = "", baud: int = 0):
        self.t = transport
        self.port_name = port_name
        self.baud = baud
        self.version = ""
        self.protocol_num = ""
        self.headers_on = False
        self.log: list[tuple[float, str, str]] = []
        self._target: tuple[int, int] | None = None
        self._t0 = time.monotonic()

    # -- low level ---------------------------------------------------------

    def _send(self, cmd: str) -> str:
        c = safety.check_command(cmd)  # raises WriteBlockedError; nothing is written
        self.t.reset_input_buffer()
        self.t.write((c + "\r").encode("ascii"))
        self._log("TX", c)
        return c

    def _read_until_prompt(self, timeout: float) -> str:
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self.t.read(max(1, self.t.in_waiting))
            if chunk:
                buf += chunk
                if b">" in buf:
                    break
        else:
            raise AdapterError("error.adapter_timeout", seconds=timeout)
        text = buf.split(b">", 1)[0].decode("ascii", "replace").replace("\x00", "")
        self._log("RX", text.strip())
        return text

    def _log(self, direction: str, text: str) -> None:
        if len(self.log) < 20000:
            self.log.append((round(time.monotonic() - self._t0, 3), direction, text))

    def command(self, cmd: str, timeout: float = 2.0) -> list[str]:
        """Send a command and return the response lines (echo and blanks removed)."""
        sent = self._send(cmd)
        raw = self._read_until_prompt(timeout)
        lines = []
        for line in re.split(r"[\r\n]+", raw):
            line = line.strip().upper()
            if not line or line.replace(" ", "") == sent:
                continue
            lines.append(line)
        return lines

    # -- setup -------------------------------------------------------------

    def initialize(self) -> None:
        """Reset the adapter and find the vehicle protocol."""
        self.command("ATZ", timeout=4.0)
        time.sleep(0.3)
        for c in ("ATE0", "ATL0", "ATS1", "ATH0", "ATAT1"):
            self.command(c)
        self.headers_on = False
        self.version = " ".join(self.command("ATI")) or "unknown"
        self.command("ATSP0")
        lines = self.command("0100", timeout=12.0)  # auto protocol search can take a while
        joined = " ".join(lines)
        if "UNABLE TO CONNECT" in joined or "NO DATA" in joined or not any("41" in l for l in lines):
            raise AdapterError("error.no_vehicle")
        dpn = "".join(self.command("ATDPN"))  # e.g. "A6" = auto-detected protocol 6
        self.protocol_num = dpn[-1:]
        if self.is_can:
            self.command("ATH1")
            self.headers_on = True

    @property
    def is_can(self) -> bool:
        return self.protocol_num in ("6", "7", "8", "9")

    @property
    def is_29bit(self) -> bool:
        return self.protocol_num in ("7", "9")

    @property
    def protocol_name(self) -> str:
        return PROTOCOLS.get(self.protocol_num, f"protocol {self.protocol_num or '?'}")

    def battery_voltage(self) -> float | None:
        try:
            txt = " ".join(self.command("ATRV"))
        except AdapterError:
            return None
        m = re.search(r"(\d+(\.\d+)?)", txt)
        return float(m.group(1)) if m else None

    def set_timeout_ms(self, ms: int) -> None:
        self.command(f"ATST{max(1, min(255, ms // 4)):02X}")

    # -- addressing ----------------------------------------------------------

    def set_target(self, tx: int, rx: int) -> None:
        """Address one ECU directly (physical addressing, 11-bit CAN)."""
        if self._target == (tx, rx):
            return
        self.command(f"ATSH{tx:03X}")
        self.command(f"ATCRA{rx:03X}")
        self.command(f"ATFCSH{tx:03X}")
        self.command("ATFCSD300000")
        self.command("ATFCSM1")
        self._target = (tx, rx)

    def reset_target(self) -> None:
        """Back to the standard OBD-II broadcast address."""
        if self._target is None:
            return
        self.command("ATSH18DB33F1" if self.is_29bit else "ATSH7DF")
        self.command("ATCRA")
        self.command("ATFCSM0")
        self._target = None

    # -- requests ----------------------------------------------------------

    def request(self, payload: str, timeout: float = 3.0) -> list[Message]:
        """Send a diagnostic request; return every response message (possibly none)."""
        lines = self.command(payload, timeout=timeout)
        lines = [l for l in lines if not l.startswith("SEARCHING") and not l.endswith("...OK")]
        if any(l == "NO DATA" for l in lines):
            return []
        if any(l == "?" for l in lines):
            raise AdapterError("error.adapter_rejected", request=payload)
        for l in lines:
            if any(l.startswith(m) for m in _ERROR_MARKERS):
                raise AdapterError("error.bus", detail=l, request=payload)
        if self.is_can and self.headers_on:
            msgs = self._parse_can(lines)
        else:
            msgs = self._parse_plain(lines)
        # Drop "response pending" placeholders when the real answer also arrived.
        real = [m for m in msgs if m.nrc != 0x78]
        return real or msgs

    def _parse_can(self, lines: list[str]) -> list[Message]:
        out: list[Message] = []
        partial: dict[str, tuple[int, bytearray]] = {}
        for line in lines:
            toks = line.split()
            if len(toks) < 2:
                continue
            if len(toks[0]) == 3:
                hdr, rest = toks[0], toks[1:]
            elif self.is_29bit and len(toks) >= 5:
                hdr, rest = "".join(toks[:4]), toks[4:]
            else:
                continue
            try:
                b = bytes(int(x, 16) for x in rest)
            except ValueError:
                continue
            if not b:
                continue
            kind = b[0] >> 4
            if kind == 0:  # single frame
                n = b[0] & 0x0F
                out.append(Message(hdr, b[1:1 + n]))
            elif kind == 1 and len(b) >= 2:  # first frame
                n = ((b[0] & 0x0F) << 8) | b[1]
                partial[hdr] = (n, bytearray(b[2:]))
            elif kind == 2 and hdr in partial:  # consecutive frame
                n, buf = partial[hdr]
                buf += b[1:]
                if len(buf) >= n:
                    out.append(Message(hdr, bytes(buf[:n])))
                    del partial[hdr]
        for hdr, (n, buf) in partial.items():  # incomplete multi-frame: keep what we have
            out.append(Message(hdr, bytes(buf)))
        return out

    def _parse_plain(self, lines: list[str]) -> list[Message]:
        out: list[Message] = []
        multi = bytearray()
        multi_len = None
        for line in lines:
            if re.fullmatch(r"[0-9A-F]{3}", line):
                multi_len = int(line, 16)
                multi = bytearray()
                continue
            m = re.fullmatch(r"([0-9A-F]):\s*(.*)", line)
            if m and multi_len is not None:
                multi += bytes.fromhex(m.group(2).replace(" ", ""))
                continue
            if _HEXLINE.match(line):
                out.append(Message(None, bytes.fromhex(line.replace(" ", ""))))
        if multi_len is not None:
            out.append(Message(None, bytes(multi[:multi_len])))
        return out

    def close(self) -> None:
        try:
            self.reset_target()
        except Exception:
            pass
        self.t.close()


# -- discovery --------------------------------------------------------------

BAUD_RATES = (115200, 38400, 500000, 230400, 57600, 9600)

# USB-serial chips used by OBD adapters: FTDI, Silicon Labs, WCH CH340, Prolific.
_ADAPTER_VIDS = {0x0403, 0x10C4, 0x1A86, 0x067B}


def list_ports() -> list[dict]:
    from serial.tools import list_ports as lp

    ports = []
    for p in lp.comports():
        desc = f"{p.description or ''} {p.manufacturer or ''}".strip()
        likely = (p.vid in _ADAPTER_VIDS) or bool(re.search(r"vlinker|obd|elm|ftdi|usb serial", desc, re.I))
        ports.append({"device": p.device, "description": desc or p.device, "likely": likely})
    ports.sort(key=lambda d: (not d["likely"], d["device"]))
    return ports


def _probe(port: str, baud: int) -> ELM327 | None:
    import serial

    try:
        s = serial.Serial(port, baud, timeout=0.05, write_timeout=1)
    except (serial.SerialException, OSError):
        return None
    elm = ELM327(s, port, baud)
    try:
        s.write(b"\r")
        time.sleep(0.1)
        s.reset_input_buffer()
        reply = " ".join(elm.command("ATI", timeout=1.0))
        if re.search(r"ELM|VLINKER|STN|OBD", reply, re.I):
            elm.version = reply
            return elm
    except (AdapterError, serial.SerialException, OSError):
        pass
    s.close()
    return None


def open_adapter(port: str | None = None, baud: int | None = None) -> ELM327:
    """Find the adapter (trying likely ports and baud rates) and return a connected driver."""
    candidates = [port] if port else [p["device"] for p in list_ports()]
    if not candidates:
        raise AdapterError("error.no_ports")
    for dev in candidates:
        for b in ([baud] if baud else BAUD_RATES):
            elm = _probe(dev, b)
            if elm:
                return elm
    raise AdapterError("error.adapter_not_found", ports=", ".join(candidates))
