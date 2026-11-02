"""DTC descriptions and status decoding.

Generic (SAE J2012 / ISO 15031-6) codes have standard meanings; their text
lives in the locale files under "dtc.<code>". Manufacturer-specific codes
(P1xxx, B/C/U codes from body modules, etc.) are only described generically
here; the AI analysis interprets them.
"""

from __future__ import annotations

from .i18n import has, msg


def describe(code: str) -> tuple[dict, bool]:
    """Return (description message, is_generic)."""
    key = f"dtc.{code}"
    if has(key):
        return msg(key), True
    system = msg(f"system_letter.{code[:1]}")
    # SAE J2012: x0xxx (and P2xxx) are generic; the other ranges are manufacturer-defined.
    second = code[1:2]
    if second == "0" or (code[:1] == "P" and second == "2"):
        return msg("dtc.no_description", system=system), False
    return msg("dtc.manufacturer", system=system), False


def failure_type(ftb: int) -> dict:
    """SAE J2012 / ISO 15031-6 failure type byte (the 3rd byte of a UDS DTC)."""
    key = f"ftb.{ftb:02X}"
    return msg(key) if has(key) else msg("ftb.unknown", code=f"0x{ftb:02X}")


def describe_uds_status(status: int) -> dict:
    """ISO 14229 DTC status byte."""
    return {
        "active_now": bool(status & 0x01),
        "failed_this_cycle": bool(status & 0x02),
        "pending": bool(status & 0x04),
        "confirmed": bool(status & 0x08),
        "failed_since_clear": bool(status & 0x20),
        "warning_lamp": bool(status & 0x80),
    }


def describe_kwp_status(status: int) -> dict:
    """ISO 14230-3 statusOfDTC byte (used by older Renault ECUs over KWP2000)."""
    storage = (status >> 5) & 0x03
    return {
        "active_now": storage == 0x03,
        "stored": storage in (0x01, 0x03),
        "intermittent": storage == 0x02,
        "warning_lamp": bool(status & 0x80),
        "symptom": status & 0x1F,
    }


def state_id(st: dict) -> str:
    """One word for the DTC state; its text is "state.<id>" in the locale files."""
    if st.get("active_now"):
        return "active"
    if st.get("confirmed") or st.get("stored"):
        return "stored"
    if st.get("pending") or st.get("intermittent"):
        return "pending"
    if st.get("failed_since_clear"):
        return "failed_earlier"
    return "recorded"
