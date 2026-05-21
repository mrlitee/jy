"""ELM327 AT-command driver. Handles init, protocol negotiation, and request/response."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

from .transport import Transport

log = logging.getLogger(__name__)

_HEX_LINE = re.compile(rb"^[0-9A-Fa-f \t]+$")


@dataclass
class ElmResponse:
    raw: str            # full text returned (lines joined with \n)
    frames: list[bytes] # decoded hex frames (one per ECU response line)
    ok: bool
    error: str = ""


class Elm327:
    """High-level ELM327 driver. Sends AT/OBD commands and decodes responses."""

    PROMPT = b">"

    def __init__(self, transport: Transport):
        self.transport = transport
        self.protocol: Optional[str] = None  # e.g. "6" -> ISO 15765-4 CAN 11/500
        self.headers_on = False

    # ---------------- low-level ----------------
    def _command(self, cmd: str, timeout: float = 2.0) -> ElmResponse:
        if not self.transport.is_open:
            self.transport.open()
        log.debug(">> %s", cmd)
        self.transport.write((cmd + "\r").encode("ascii"))
        raw = self.transport.read_until(self.PROMPT, timeout=timeout)
        text = raw.decode("ascii", errors="ignore").replace("\r", "\n")
        text = text.replace(">", "").strip()
        log.debug("<< %s", text)
        return self._parse(text)

    @staticmethod
    def _parse(text: str) -> ElmResponse:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        # ELM error tokens
        errors = {
            "NO DATA", "ERROR", "?", "STOPPED", "BUS BUSY",
            "BUS ERROR", "CAN ERROR", "FB ERROR", "DATA ERROR",
            "UNABLE TO CONNECT", "BUFFER FULL", "ACT ALERT",
        }
        bad = next((ln for ln in lines if ln.upper() in errors or ln.upper().startswith("UNABLE")), None)
        if bad:
            return ElmResponse(raw=text, frames=[], ok=False, error=bad)

        frames: list[bytes] = []
        for ln in lines:
            # Skip echoes ("ATZ"), empty, or non-hex info lines
            if not all(c in "0123456789ABCDEFabcdef \t" for c in ln):
                continue
            try:
                frames.append(bytes.fromhex(ln.replace(" ", "")))
            except ValueError:
                continue
        return ElmResponse(raw=text, frames=frames, ok=True)

    # ---------------- init ----------------
    def initialize(self, protocol: str = "0") -> bool:
        """Bring the adapter into a known state and auto-detect the protocol.

        protocol="0" means automatic. Other values follow ATSP table:
            1=J1850 PWM, 2=J1850 VPW, 3=ISO 9141-2, 4=ISO 14230-4 KWP 5baud,
            5=ISO 14230-4 KWP fast, 6=CAN 11/500, 7=CAN 29/500,
            8=CAN 11/250, 9=CAN 29/250, A=SAE J1939
        """
        # reset
        self._command("ATZ", timeout=4.0)
        time.sleep(0.4)
        # echo off, linefeeds off, spaces off, headers off, adaptive timing aggressive
        for cmd in ("ATE0", "ATL0", "ATS0", "ATH0", "ATAT2", "ATST64"):
            r = self._command(cmd)
            if not r.ok:
                log.warning("init cmd %s failed: %s", cmd, r.error)
        # set protocol
        r = self._command(f"ATSP{protocol}")
        if not r.ok:
            return False
        # warm-up: send 0100 to force protocol detection
        r = self._command("0100", timeout=5.0)
        if not r.ok:
            log.warning("0100 failed during init: %s", r.error)
        # query active protocol
        rp = self._command("ATDPN")
        if rp.ok and rp.raw:
            self.protocol = rp.raw.strip().lstrip("A")
        return True

    def set_headers(self, on: bool) -> None:
        self._command("ATH1" if on else "ATH0")
        self.headers_on = on

    def set_can_id(self, header: str) -> None:
        """Set tester CAN header (e.g. '7E0' for 11-bit, '18DB33F1' for 29-bit)."""
        self._command(f"ATSH{header}")

    # ---------------- OBD requests ----------------
    def request(self, mode_pid: str, timeout: float = 1.5) -> ElmResponse:
        """Send an OBD request like '0100', '03', '22F190'. Returns parsed response."""
        return self._command(mode_pid, timeout=timeout)

    def voltage(self) -> Optional[float]:
        r = self._command("ATRV")
        m = re.search(r"(\d+\.\d+)", r.raw)
        return float(m.group(1)) if m else None

    def device_id(self) -> str:
        return self._command("ATI").raw.strip()

    def close(self) -> None:
        try:
            self._command("ATPC")  # protocol close
        except Exception:
            pass
        self.transport.close()
