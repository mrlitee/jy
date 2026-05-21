"""Diagnostic Trouble Code parser (Mode 03 / 07 / 0A) + multi-brand description lookup."""
from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Iterable

# DTC first nibble -> system letter (ISO 15031-6)
_SYSTEM = {0: "P", 1: "C", 2: "B", 3: "U"}
# DTC second nibble -> 0=generic (P0xxx), 1=manufacturer (P1xxx), 2/3=ISO/SAE reserved
_KIND = {0: 0, 1: 1, 2: 2, 3: 3}


@dataclass(frozen=True)
class Dtc:
    code: str           # "P0171"
    severity: str       # "current" | "pending" | "permanent"
    description: str = ""
    brand: str = "generic"


def decode_dtc_word(word: int) -> str:
    """Convert a 16-bit raw DTC value to its 5-character text code."""
    sys_nibble = (word >> 14) & 0x3
    kind_nibble = (word >> 12) & 0x3
    rest = word & 0x0FFF
    return f"{_SYSTEM[sys_nibble]}{_KIND[kind_nibble]}{rest:03X}"


def parse_dtc_payload(frames: list[bytes], mode: str) -> list[str]:
    """Decode a Mode 03 / 07 / 0A response into DTC codes.

    Each response begins with the response-mode byte (e.g. 0x43 for Mode 03).
    On ISO 15765 (CAN) the next byte is the *count of DTCs*; ELM327 with
    ``ATCAF1`` (default) leaves it in the payload. On KWP / J1850 some clones
    omit the count byte and just emit the DTC pairs directly. We detect the
    count byte robustly: only strip it when ``count * 2 + 1 == len(body)``,
    i.e. the leading byte exactly accounts for the remaining bytes as DTC
    pairs. This avoids the legacy heuristic's blind spot where a real DTC's
    high byte (e.g. 0x01 for ``P0123``) happened to look like a count.
    """
    payload = b"".join(frames)
    resp_byte = (int(mode, 16) | 0x40).to_bytes(1, "big")
    idx = payload.find(resp_byte)
    if idx < 0:
        return []
    body = payload[idx + 1:]
    if body and body[0] * 2 + 1 == len(body):
        body = body[1:]
    codes: list[str] = []
    for i in range(0, len(body) - 1, 2):
        word = (body[i] << 8) | body[i + 1]
        if word == 0:
            continue
        codes.append(decode_dtc_word(word))
    return codes


class DtcDatabase:
    """Loads DTC description JSONs lazily and resolves codes to human text."""

    _BRANDS = ("generic", "honda", "yamaha", "suzuki", "kawasaki")

    def __init__(self) -> None:
        self._tables: dict[str, dict[str, str]] = {}

    def _load(self, brand: str) -> dict[str, str]:
        if brand in self._tables:
            return self._tables[brand]
        try:
            data = files("pedaku.data").joinpath(f"dtc_{brand}.json").read_text(encoding="utf-8")
            self._tables[brand] = json.loads(data)
        except FileNotFoundError:
            self._tables[brand] = {}
        return self._tables[brand]

    def describe(self, code: str, brand: str = "generic") -> str:
        # Try brand first, then generic; manufacturer codes (P1xxx, U1xxx) are usually brand-specific.
        for b in (brand, "generic"):
            desc = self._load(b).get(code)
            if desc:
                return desc
        return "Unknown DTC - consult service manual"

    def enrich(self, codes: Iterable[str], severity: str, brand: str = "generic") -> list[Dtc]:
        return [Dtc(code=c, severity=severity, description=self.describe(c, brand), brand=brand) for c in codes]
