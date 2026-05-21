"""ELM327 AT-command driver. Handles init, protocol negotiation, and request/response.

Robustness contract
-------------------
Every command goes through :meth:`_command`, which:

1. Drains any leftover bytes on the transport before writing (so a stray
   prompt from a previous cancelled command doesn't make the next reply
   look corrupt).
2. Catches :class:`TransportError` from the underlying socket / serial,
   tries one full reopen + retry, and only then surfaces the failure.
3. Returns a :class:`ElmResponse` whose ``ok`` flag tells callers whether
   the ECU answered with usable data, while leaving the *transport* alive
   for the next command. NO DATA / BUS BUSY / "?" do NOT count as
   transport failures - those are normal ECU responses.

Exposes a ``failure_streak`` counter the LiveSampler uses for its
watchdog: too many consecutive transport-level failures flip the session
to disconnected so the UI gets a clean signal.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

from .transport import Transport, TransportError

log = logging.getLogger(__name__)

# A line that is purely hex (with optional whitespace).
_HEX_LINE = re.compile(r"^[0-9A-Fa-f][0-9A-Fa-f \t]*$")
# Multi-frame CAN line numbering prefix that ELM327 prepends to each frame
# of a long response (e.g. ``0:4902014743564B``, ``1:4D41364C39444A``).
# It's a single hex digit + ``:`` at the start of the line. We strip it so
# the surrounding hex parser can concatenate the frames into one payload.
_CAN_LINE_PREFIX = re.compile(r"^[0-9A-Fa-f]:\s*")
# KWP responses sometimes come back as ``HH HH : HH HH HH ...`` with a
# colon between header and payload bytes. We don't use headers (ATH0), but
# strip any stray colons just in case some clones leave them in.
_STRAY_COLON = re.compile(r"\s*:\s*")


@dataclass
class ElmResponse:
    raw: str            # full text returned (lines joined with \n)
    frames: list[bytes] # decoded hex frames (one per ECU response line)
    ok: bool
    error: str = ""


class Elm327:
    """High-level ELM327 driver. Sends AT/OBD commands and decodes responses."""

    PROMPT = b">"
    # How many consecutive transport-level failures we let the LiveSampler
    # watchdog see before it tears down the session. The driver itself only
    # retries once per command; this counter is read externally.
    failure_streak: int = 0

    def __init__(self, transport: Transport):
        self.transport = transport
        self.protocol: Optional[str] = None  # e.g. "6" -> ISO 15765-4 CAN 11/500
        self.headers_on = False

    # ---------------- low-level ----------------
    def _command(self, cmd: str, timeout: float = 2.0) -> ElmResponse:
        """Send a command, wait for the next ELM327 prompt, parse the reply.

        On a transport-level failure (socket reset, serial gone) we close
        the transport, reopen it, and try ONE more time. A second failure
        is surfaced as a non-OK :class:`ElmResponse` so HTTP handlers can
        report a clean error instead of bubbling an OSError up the stack.
        """
        for attempt in range(2):
            if not self.transport.is_open:
                try:
                    self.transport.open()
                except TransportError as ex:
                    self.failure_streak += 1
                    if attempt == 1:
                        return ElmResponse(raw="", frames=[], ok=False,
                                            error=f"transport reopen failed: {ex}")
                    continue
            try:
                # Drain any stale prompt bytes so the next read can't latch
                # onto a previous-command terminator and return early.
                self.transport.drain()
                log.debug(">> %s", cmd)
                self.transport.write((cmd + "\r").encode("ascii"))
                raw = self.transport.read_until(self.PROMPT, timeout=timeout)
            except TransportError as ex:
                log.warning("transport error on %r (attempt %d): %s",
                            cmd, attempt + 1, ex)
                self.failure_streak += 1
                # Try once more after a forced reopen; a flaky WiFi link or
                # a powered-down adapter sometimes recovers within a second.
                try:
                    self.transport.reopen()
                except TransportError as reopen_ex:
                    if attempt == 1:
                        return ElmResponse(raw="", frames=[], ok=False,
                                            error=f"transport dead: {reopen_ex}")
                    continue
                if attempt == 1:
                    return ElmResponse(raw="", frames=[], ok=False,
                                        error=f"transport error: {ex}")
                continue
            text = raw.decode("ascii", errors="ignore").replace("\r", "\n")
            text = text.replace(">", "").strip()
            log.debug("<< %s", text)
            response = self._parse(text)
            # ECU said NO DATA / "?" -> link is fine, command just didn't
            # apply. Reset the streak so a single missing PID doesn't trip
            # the session watchdog.
            self.failure_streak = 0
            return response
        # Unreachable but keeps mypy happy.
        return ElmResponse(raw="", frames=[], ok=False, error="unreachable")

    @staticmethod
    def _parse(text: str) -> ElmResponse:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        # ELM error tokens
        errors = {
            "NO DATA", "ERROR", "?", "STOPPED", "BUS BUSY",
            "BUS ERROR", "CAN ERROR", "FB ERROR", "DATA ERROR",
            "UNABLE TO CONNECT", "BUFFER FULL", "ACT ALERT",
        }
        bad = next(
            (ln for ln in lines if ln.upper() in errors or ln.upper().startswith("UNABLE")),
            None,
        )
        if bad:
            return ElmResponse(raw=text, frames=[], ok=False, error=bad)

        # On CAN, long ECU responses (>7 bytes) come back with a per-line
        # ``<idx>:<hex>`` prefix that ELM327 inserts for each frame of the
        # multi-frame OBD payload. Such lines must be concatenated into ONE
        # bytes frame so parse_response sees the contiguous payload.
        #
        # Plain hex lines, on the other hand, are independent ECU frames
        # (typical for J1850 / KWP, single-frame CAN, or the optional 1-byte
        # length header some clones print before a multi-frame burst).
        # Each plain hex line therefore becomes its own ``frames[]`` entry
        # and we flush any in-progress multi-frame run when we see one.
        frames: list[bytes] = []
        multi_run: list[str] = []

        def _flush_multi() -> None:
            if not multi_run:
                return
            joined = "".join(multi_run).replace(" ", "").replace("\t", "")
            # Some clones emit an odd nibble count when stripping trailing
            # padding; pad with a leading zero rather than discarding the
            # whole frame.
            if len(joined) % 2 == 1:
                joined = "0" + joined
            try:
                frames.append(bytes.fromhex(joined))
            except ValueError:
                pass
            multi_run.clear()

        for ln in lines:
            m = _CAN_LINE_PREFIX.match(ln)
            if m:
                # CAN-numbered frame; accumulate.
                stripped = ln[m.end():].replace(" ", "").replace("\t", "")
                if _HEX_LINE.match(stripped):
                    multi_run.append(stripped)
                else:
                    _flush_multi()
                continue
            # Non-prefixed line ends any multi-frame run.
            _flush_multi()
            stripped = _STRAY_COLON.sub("", ln).replace(" ", "").replace("\t", "")
            if _HEX_LINE.match(stripped):
                try:
                    frames.append(bytes.fromhex(stripped))
                except ValueError:
                    pass
            # Echo / banner / "SEARCHING..." lines are silently dropped.
        _flush_multi()
        return ElmResponse(raw=text, frames=frames, ok=True)

    # ---------------- init ----------------
    def _wait_prompt(self, timeout: float = 4.0) -> bool:
        """After ``ATZ`` the adapter prints a banner, then a ``>``. Older
        clones can take 1.5+ seconds to do that and a fixed sleep was the
        old root cause of "ELM327 init failed (no protocol detected)" on
        slow adapters: we'd send ``ATE0`` while ``ATZ``'s banner was still
        in flight, the adapter would echo our command back into the banner,
        and every subsequent reply was misaligned.
        """
        if not self.transport.is_open:
            try:
                self.transport.open()
            except TransportError as ex:
                log.warning("transport open in _wait_prompt failed: %s", ex)
                return False
        try:
            data = self.transport.read_until(self.PROMPT, timeout=timeout)
        except TransportError as ex:
            log.warning("waiting for prompt failed: %s", ex)
            return False
        return self.PROMPT in data

    def initialize(self, protocol: str = "0") -> bool:
        """Bring the adapter into a known state and auto-detect the protocol.

        protocol="0" means automatic. Other values follow ATSP table:
            1=J1850 PWM, 2=J1850 VPW, 3=ISO 9141-2, 4=ISO 14230-4 KWP 5baud,
            5=ISO 14230-4 KWP fast, 6=CAN 11/500, 7=CAN 29/500,
            8=CAN 11/250, 9=CAN 29/250, A=SAE J1939
        """
        # reset
        self._command("ATZ", timeout=4.0)
        # Wait for the post-banner prompt instead of sleeping a fixed 0.4s.
        # Cheap clones routinely take 1-2s to print their boot banner; we
        # used to clobber that banner with the next command.
        self._wait_prompt(timeout=2.0)
        # echo off, linefeeds off, spaces off, headers off, adaptive timing aggressive
        for cmd in ("ATE0", "ATL0", "ATS0", "ATH0", "ATAT2", "ATST64"):
            r = self._command(cmd)
            if not r.ok:
                log.warning("init cmd %s failed: %s", cmd, r.error)
        # set protocol
        r = self._command(f"ATSP{protocol}")
        if not r.ok:
            return False
        # warm-up: send 0100 to force protocol detection. Some adapters
        # answer "SEARCHING..." for a couple of seconds before the first
        # real reply, so allow a longer timeout here.
        r = self._command("0100", timeout=6.0)
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
        try:
            self.transport.close()
        except Exception as ex:  # pragma: no cover - defensive
            log.debug("transport close error: %s", ex)
