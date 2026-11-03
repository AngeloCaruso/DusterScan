"""Turning a module's reply bytes into named values, the way DDT4All does.

A request lists its data items with a position: ``firstbyte`` (1-based, the
reply's first byte is the positive response code) and ``bitoffset`` (counted
from the most significant bit of that byte). The item's definition gives its
size and how to show it: named states (``lists``), text (``bytesascii``),
hex (``byte``), or a number ``(raw * step + offset) / divideby``.

Every value is checked against the reply: if the reply does not start the
way the definition expects, is too short, or the item falls outside it, the
value is dropped rather than guessed.
"""

from __future__ import annotations

import re

from .store import repair

_CODE_PREFIX = re.compile(r"^\s*(0x[0-9A-Fa-f]+|[01]+b?)\s*=\s*")


def bits_of(data_def: dict) -> int:
    if data_def.get("bitscount"):
        return int(data_def["bitscount"])
    return int(data_def.get("bytescount", 1)) * 8


def expected_header(sent: bytes) -> bytes:
    """The fixed start of a positive reply: response code + the identifier asked for."""
    sid = sent[0]
    id_len = {0x22: 3, 0x21: 2, 0x19: 2, 0x1A: 2}.get(sid, 1)
    return bytes([sid + 0x40]) + sent[1:id_len]


def reply_ok(request: dict, reply: bytes) -> bool:
    try:
        sent = bytes.fromhex(request["sentbytes"])
    except (ValueError, KeyError):
        return False
    return bool(sent) and reply.startswith(expected_header(sent)) and len(reply) >= int(request.get("minbytes", 1))


def raw_value(reply: bytes, firstbyte: int, bitoffset: int, bits: int, little: bool) -> int | None:
    start = firstbyte - 1
    span = (bitoffset + bits + 7) // 8
    if start < 0 or bits <= 0 or start + span > len(reply):
        return None
    chunk = reply[start:start + span]
    if little and bitoffset == 0 and bits % 8 == 0:
        chunk = chunk[::-1]
    value = int.from_bytes(chunk, "big")
    return (value >> (span * 8 - bitoffset - bits)) & ((1 << bits) - 1)


def _number(value: float, data_def: dict):
    fmt = str(data_def.get("format", ""))
    if "." in fmt:
        return round(value, len(fmt.split(".", 1)[1]))
    if float(value).is_integer():
        return int(value)
    return round(value, 3)


def decode_item(data_def: dict, ref: dict, reply: bytes, ecu_little: bool = False):
    """One value (number or text), or None if it is not in the reply."""
    bits = bits_of(data_def)
    endian = ref.get("endian") or data_def.get("endian")
    little = endian == "Little" if endian else ecu_little
    raw = raw_value(reply, int(ref.get("firstbyte", 1)), int(ref.get("bitoffset", 0)), bits, little)
    if raw is None:
        return None
    if data_def.get("bytesascii"):
        n = (bits + 7) // 8
        text = raw.to_bytes(n, "big").decode("latin-1")
        return "".join(c for c in text if c.isprintable()).strip()
    lists = data_def.get("lists") or {}
    if lists and not data_def.get("scaled"):
        label = lists.get(str(raw))
        return _CODE_PREFIX.sub("", repair(label)).strip() if label is not None else str(raw)
    if data_def.get("byte") and not data_def.get("scaled"):
        return f"{raw:0{(bits + 3) // 4}X}"
    if data_def.get("signed") and raw & (1 << (bits - 1)):
        raw -= 1 << bits
    if data_def.get("scaled"):
        value = (raw * float(data_def.get("step", 1)) + float(data_def.get("offset", 0))) \
            / float(data_def.get("divideby", 1) or 1)
        return _number(value, data_def)
    return raw


def decode_request(definition: dict, request: dict, reply: bytes) -> dict:
    """All values of one reply, by item name. Empty if the reply does not fit the request."""
    if not reply_ok(request, reply):
        return {}
    data = definition.get("data", {})
    ecu_little = definition.get("endian") == "Little"
    out = {}
    for name, ref in (request.get("receivebyte_dataitems") or {}).items():
        d = data.get(name)
        if d is None:
            continue
        v = decode_item(d, ref, reply, ecu_little)
        if v is not None:
            out[name] = v
    return out


def unit_of(definition: dict, item: str) -> str:
    unit = repair(str((definition.get("data", {}).get(item) or {}).get("unit", ""))).strip()
    return unit.replace("�C", "°C")  # the degree sign was lost in some files
