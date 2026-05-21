"""Protocol-aware helpers built on top of the ELM327 driver.

ELM327 already abstracts most of the wire protocol. This module records useful
brand-specific session presets (init bytes, headers, Mode 22 PID maps).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .elm327 import Elm327


@dataclass
class BrandProfile:
    """Brand-specific session config used at connect time."""
    name: str
    elm_protocol: str = "0"        # "0" = auto; otherwise see ATSP table
    tester_header: Optional[str] = None  # e.g. "7E0" for 11-bit CAN tester
    init_commands: list[str] = field(default_factory=list)
    # Brand Mode 22 PID extensions: code -> (name, unit, nbytes, scale, offset)
    extra_pids: dict[str, tuple[str, str, int, float, float]] = field(default_factory=dict)


# Reasonable defaults. Real-world headers vary per model year; these are starting
# points that work on the majority of injection-ECU motorcycles in each family.
PROFILES: dict[str, BrandProfile] = {
    "Generic OBD-II":  BrandProfile("Generic OBD-II", elm_protocol="0"),
    # Honda KWP fast init (protocol 5). ATSI is intentionally NOT here -
    # it requests a SLOW init which is only valid on protocols 3 and 4 and
    # would make the ELM327 reply with "?" on protocol 5. The remaining
    # commands set the tester header, the ISO init address, and the wakeup
    # message that keeps the KWP session alive between requests.
    "Honda":           BrandProfile("Honda", elm_protocol="5",
                                    init_commands=["ATSH8110F1", "ATIIA10", "ATWM8110F13E"]),
    "Yamaha":          BrandProfile("Yamaha", elm_protocol="5",
                                    init_commands=["ATSH8112F1", "ATIIA12"]),
    "Suzuki":          BrandProfile("Suzuki", elm_protocol="5",
                                    init_commands=["ATSH8110F1", "ATIIA10"]),
    "Kawasaki":        BrandProfile("Kawasaki", elm_protocol="5",
                                    init_commands=["ATSH8111F1", "ATIIA11"]),
    "KTM (CAN)":       BrandProfile("KTM (CAN)", elm_protocol="6", tester_header="7E0"),
    "BMW Motorrad":    BrandProfile("BMW Motorrad", elm_protocol="6", tester_header="6F1"),
    "Ducati":          BrandProfile("Ducati", elm_protocol="6", tester_header="7E0"),
}


class ProtocolSession:
    """Apply a BrandProfile to an open ELM327 driver."""

    def __init__(self, elm: Elm327, profile: BrandProfile):
        self.elm = elm
        self.profile = profile

    def apply(self) -> bool:
        if not self.elm.initialize(self.profile.elm_protocol):
            return False
        if self.profile.tester_header:
            self.elm.set_can_id(self.profile.tester_header)
        for cmd in self.profile.init_commands:
            self.elm._command(cmd)  # noqa: SLF001 - internal helper
        return True
