"""Read-only guard.

Every command that leaves this program for the adapter passes through
``check_command``. It works as an allowlist: a command is sent only if it
matches one of the explicitly permitted read operations below. Anything
else - including commands this file has never heard of - is refused before
a single byte reaches the serial port.

What is allowed:
  * ELM327 "AT" commands that configure the *adapter* (echo, headers,
    protocol, which CAN ID to talk to, timeouts). None of these touch the car.
  * OBD-II read modes 01, 02, 03, 06, 07, 09, 0A.
  * UDS / KWP2000 read services: session control (default/extended only),
    tester present, read DTCs, read identification, read data by identifier.

What is refused (non-exhaustive, everything not listed above is refused):
  * OBD mode 04 (clear DTCs / reset MIL) and mode 08 (control on-board system)
  * 0x14 ClearDiagnosticInformation, 0x2E/0x3B write data, 0x3D write memory,
    0x31 routines, 0x27 security access, 0x2F/0x30 actuator control,
    0x34-0x37 software download, 0x11 ECU reset, 0x28 communication control,
    0x85 DTC setting control, programming sessions (0x10 02 / 0x10 85).
  * AT commands that write the adapter's own memory (AT PP, AT SD) or that
    are not needed for reading (monitor modes, baud changes, ...).
  * Requests to CAN IDs outside the diagnostic range.
"""

from __future__ import annotations

import re


class WriteBlockedError(Exception):
    """Raised when a command is not on the read-only allowlist."""


# --- Adapter (AT) commands -------------------------------------------------

_HEX = "[0-9A-F]"

_ALLOWED_AT = [re.compile(p) for p in (
    r"^AT(Z|WS|D|I|@1|RV|DP|DPN|PC|AR|E[01]|L[01]|S[01]|H[01]|CAF[01]|CFC[01]|AT[012])$",
    rf"^ATSPA?{_HEX}$",                       # select protocol
    rf"^ATST{_HEX}{{2}}$",                    # response timeout
    rf"^ATSH({_HEX}{{3}}|{_HEX}{{6}}|{_HEX}{{8}})$",   # target header
    rf"^ATCRA({_HEX}{{3}}|{_HEX}{{8}})?$",    # receive filter
    rf"^ATFCSH({_HEX}{{3}}|{_HEX}{{8}})$",    # ISO-TP flow control header
    rf"^ATFCSD({_HEX}{{2}}){{1,5}}$",         # flow control data
    r"^ATFCSM[0-2]$",                         # flow control mode
)]


def _check_header(cmd: str) -> None:
    """Only allow talking to diagnostic CAN identifiers."""
    m = re.match(r"^AT(SH|FCSH)(.+)$", cmd)
    if not m:
        return
    hdr = m.group(2)
    if len(hdr) == 3:
        if not 0x700 <= int(hdr, 16) <= 0x7FF:
            raise WriteBlockedError(f"CAN ID {hdr} is outside the diagnostic range 700-7FF")
    elif len(hdr) == 8:
        # 29-bit diagnostic addressing: 18DA<target><source> or functional 18DB33F1
        if not (hdr.startswith("18DA") or hdr == "18DB33F1"):
            raise WriteBlockedError(f"29-bit header {hdr} is not a diagnostic address")
    # 6-character headers are K-line / J1850 style (priority, target, source); allowed.


# --- Vehicle requests --------------------------------------------------------

# OBD-II modes that only read.
_OBD_READ_MODES = {0x01, 0x02, 0x03, 0x06, 0x07, 0x09, 0x0A}

# UDS (ISO 14229) and KWP2000 (ISO 14230) services that only read.
_READ_SERVICES = {
    0x17: "ReadStatusOfDTC (KWP)",
    0x18: "ReadDTCByStatus (KWP)",
    0x19: "ReadDTCInformation (UDS)",
    0x1A: "ReadECUIdentification (KWP)",
    0x21: "ReadDataByLocalIdentifier (KWP)",
    0x22: "ReadDataByIdentifier (UDS)",
    0x3E: "TesterPresent",
}

# Diagnostic sessions we may enter. Default and extended sessions only change
# what the ECU is willing to *answer*; they expire on their own. Programming
# sessions (0x02, 0x85) are never allowed.
_ALLOWED_SESSIONS = {0x01, 0x03, 0x81, 0xC0}

# Names for the most important refused services, for clear error messages.
BLOCKED_SERVICE_NAMES = {
    0x04: "Clear DTCs / reset MIL (OBD mode 04)",
    0x08: "Control on-board system (OBD mode 08)",
    0x10: "Programming session",
    0x11: "ECU reset",
    0x14: "Clear diagnostic information",
    0x23: "Read memory by address",
    0x27: "Security access",
    0x28: "Communication control",
    0x2E: "Write data by identifier",
    0x2F: "Input/output control",
    0x30: "Input/output control by local identifier",
    0x31: "Routine control",
    0x34: "Request download",
    0x35: "Request upload",
    0x36: "Transfer data",
    0x37: "Request transfer exit",
    0x3B: "Write data by local identifier",
    0x3D: "Write memory by address",
    0x85: "Control DTC setting",
}

MAX_REQUEST_BYTES = 7  # single ISO-TP frame; every read we need fits


def normalize(cmd: str) -> str:
    return re.sub(r"\s+", "", cmd).upper()


def check_command(cmd: str) -> str:
    """Return the normalized command if it is allowed, else raise WriteBlockedError."""
    c = normalize(cmd)
    if not c:
        raise WriteBlockedError("empty command")

    if c.startswith("AT"):
        if not any(p.match(c) for p in _ALLOWED_AT):
            raise WriteBlockedError(f"adapter command '{c}' is not on the read-only allowlist")
        _check_header(c)
        return c

    if not re.fullmatch(r"([0-9A-F]{2})+", c):
        raise WriteBlockedError(f"'{cmd}' is not a hex request")

    data = bytes.fromhex(c)
    if len(data) > MAX_REQUEST_BYTES:
        raise WriteBlockedError(f"request '{c}' is longer than any read request ({len(data)} bytes)")

    sid = data[0]
    if sid in _OBD_READ_MODES:
        return c
    if sid == 0x10:
        if len(data) == 2 and data[1] in _ALLOWED_SESSIONS:
            return c
        raise WriteBlockedError(f"diagnostic session '{c}' is not allowed (only default/extended)")
    if sid == 0x3E:
        if len(data) <= 2 and (len(data) == 1 or data[1] in (0x00, 0x01, 0x80)):
            return c
        raise WriteBlockedError(f"tester present '{c}' has an unexpected format")
    if sid in _READ_SERVICES:
        return c

    name = BLOCKED_SERVICE_NAMES.get(sid, "unknown service")
    raise WriteBlockedError(f"service 0x{sid:02X} ({name}) is refused: this program is read-only")
