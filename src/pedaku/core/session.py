"""DiagnosticSession orchestrates transport + ELM327 + protocol profile + DTC + PIDs.

A session owns:

* one :class:`Transport` (serial / TCP / Bluetooth / mock),
* one :class:`Elm327` driver wrapping it,
* one :class:`LiveSampler` that runs the dedicated I/O thread - all ECU
  commands route through this thread to keep the UI responsive.

Lifecycle
---------
``connect()`` is idempotent and self-healing: if the transport fails on the
first attempt we close everything and retry up to :attr:`CONNECT_RETRIES`
times before giving up. The ``last_error`` field carries the human-readable
reason of the most recent failure so the UI can show it instead of a flat
"Connect failed".

While connected, the :class:`LiveSampler` calls :meth:`_on_link_dead` when
its watchdog has seen too many consecutive transport failures; the session
flips ``connected`` to ``False`` so the SSE handlers exit cleanly and the
browser shows an "offline" indicator without the user clicking anything.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .dtc import Dtc, DtcDatabase, parse_dtc_payload
from .elm327 import Elm327
from .live import LiveSampler, P_USER_HIGH, P_USER_NORM, compute_health_score
from .live_test import ActiveTest, LiveTestRunner
from .obd_pid import PIDS_01, Pid, all_live_pids, parse_response
from .protocols import PROFILES, BrandProfile, ProtocolSession
from .transport import (
    BluetoothTransport,
    SerialTransport,
    TcpTransport,
    Transport,
    TransportError,
)

log = logging.getLogger(__name__)


@dataclass
class EcuInfo:
    vin: str = ""
    ecu_name: str = ""
    voltage: Optional[float] = None
    protocol: str = ""
    adapter: str = ""


@dataclass
class FreezeFrame:
    dtc: str = ""
    values: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class DiagnosticSession:
    transport: Transport
    brand: str = "Generic OBD-II"
    elm: Elm327 = field(init=False)
    runner: LiveTestRunner = field(init=False)
    db: DtcDatabase = field(default_factory=DtcDatabase)
    info: EcuInfo = field(default_factory=EcuInfo)
    live: LiveSampler = field(init=False)
    last_error: str = ""
    _connected: bool = field(default=False, init=False, repr=False)
    _connect_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _last_dtc_count: int = field(default=0, init=False, repr=False)

    # Try the entire ATZ / ATSP / 0100 init sequence this many times before
    # giving up. Cheap clones occasionally need a kick before they answer.
    CONNECT_RETRIES: int = 2

    def __post_init__(self) -> None:
        self.elm = Elm327(self.transport)
        self.runner = LiveTestRunner(self.elm)
        # Sampler is set up lazily during connect() once we know the active PIDs.
        # We pass a heartbeat that pings ATRV (always supported) so a dead link
        # is detected even when the user is parked on a non-PID-polling tab.
        self.live = LiveSampler(
            request_fn=self.elm.request,
            pids=all_live_pids(),
            on_link_dead=self._on_link_dead,
            heartbeat_fn=self._heartbeat,
            heartbeat_interval=30.0,
        )

    # ---------------- lifecycle ----------------
    @classmethod
    def from_address(cls, address: str, kind: str, brand: str = "Generic OBD-II") -> "DiagnosticSession":
        kind = (kind or "serial").lower()
        if kind == "tcp":
            host, _, port = address.partition(":")
            transport: Transport = TcpTransport(host, int(port or "35000"))
        elif kind in ("bluetooth", "bt"):
            transport = BluetoothTransport.from_address(address)
        else:
            transport = SerialTransport(address)
        return cls(transport=transport, brand=brand)

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> bool:
        """Bring the transport up and run protocol negotiation.

        Retries the whole sequence up to :attr:`CONNECT_RETRIES` times so a
        single dropped frame at boot doesn't force the user to click
        "Connect" again. ``last_error`` is set to the most recent failure
        reason so the API can surface it instead of a generic "init failed".
        """
        with self._connect_lock:
            if self._connected:
                return True
            profile: BrandProfile = PROFILES.get(self.brand, PROFILES["Generic OBD-II"])
            last_err = ""
            for attempt in range(self.CONNECT_RETRIES + 1):
                try:
                    self.transport.open()
                except TransportError as ex:
                    last_err = f"transport open failed: {ex}"
                    log.warning("connect attempt %d/%d: %s",
                                attempt + 1, self.CONNECT_RETRIES + 1, last_err)
                    time.sleep(0.5)
                    continue
                try:
                    ok = ProtocolSession(self.elm, profile).apply()
                except Exception as ex:
                    last_err = f"protocol negotiation raised: {ex}"
                    log.exception("connect attempt %d", attempt + 1)
                    ok = False
                if not ok:
                    if not last_err:
                        last_err = "ELM327 init failed (no protocol detected)"
                    try:
                        self.transport.close()
                    except Exception:  # pragma: no cover - defensive
                        pass
                    time.sleep(0.5)
                    continue
                # Successful negotiation: gather metadata before exposing
                # the session as "connected" so /api/state never returns
                # half-populated info.
                try:
                    self.info.adapter = self.elm.device_id()
                    self.info.voltage = self.elm.voltage()
                    self.info.protocol = self.elm.protocol or ""
                    self._read_vehicle_info()
                except Exception as ex:
                    log.warning("post-init metadata read failed: %s", ex)
                self._connected = True
                self.last_error = ""
                self.live.start()
                return True
            self.last_error = last_err or "ELM327 init failed (unknown reason)"
            return False

    def disconnect(self) -> None:
        with self._connect_lock:
            if not self._connected:
                return
            self._connected = False
            try:
                self.live.stop()
            except Exception as ex:
                log.debug("live.stop raised: %s", ex)
            try:
                self.elm.close()
            except Exception as ex:
                log.debug("elm.close raised: %s", ex)

    def _on_link_dead(self, reason: str) -> None:
        """Watchdog callback: link is considered dead.

        Called from the I/O worker; we cannot block on ``_connect_lock``
        here (the worker itself might be holding the GIL while another
        thread calls disconnect()), so we just flip the flag and let the
        next disconnect / connect cycle do the cleanup.
        """
        if not self._connected:
            return
        self._connected = False
        self.last_error = reason
        log.warning("session marked offline by watchdog: %s", reason)

    def _heartbeat(self) -> bool:
        """Cheap link-alive probe used by the LiveSampler.

        ATRV is universally supported by every ELM327 / clone and is
        answered by the *adapter* itself, not the ECU - so it works even
        when the bike's ignition is off and detects a dead WiFi/BT link
        regardless of whether the engine is actually running.
        """
        try:
            r = self.elm.request("ATRV", timeout=1.5)
        except Exception as ex:
            log.debug("heartbeat raised: %s", ex)
            return False
        return bool(r.ok and r.raw)

    # ---------------- info ----------------
    def _read_vehicle_info(self) -> None:
        from .obd_pid import PIDS_09
        for code in ("02", "0A"):
            pid = PIDS_09[code]
            r = self.elm.request(pid.request, timeout=2.0)
            value = parse_response(pid, r.frames) if r.ok else None
            if value:
                if code == "02":
                    self.info.vin = str(value)
                else:
                    self.info.ecu_name = str(value)

    def health_score(self, dtc_count: Optional[int] = None) -> int:
        # By default use the cached DTC count from the last scan to keep the
        # /api/health endpoint cheap (polled every few seconds by the UI).
        # Pass an explicit value when generating reports right after a scan.
        if dtc_count is None:
            dtc_count = self._last_dtc_count
        return compute_health_score(self.live.latest_snapshot(), dtc_count)

    # ---------------- DTC ----------------
    def read_dtcs(self) -> list[Dtc]:
        def _job() -> list[Dtc]:
            out: list[Dtc] = []
            for mode, severity in (("03", "current"), ("07", "pending"), ("0A", "permanent")):
                r = self.elm.request(mode, timeout=2.0)
                if not r.ok:
                    continue
                codes = parse_dtc_payload(r.frames, mode)
                out.extend(self.db.enrich(codes, severity, self.brand.lower()))
            return out
        result = self.live.submit(_job, priority=P_USER_HIGH, timeout=8.0)
        self._last_dtc_count = len(result)
        return result

    def clear_dtcs(self) -> bool:
        ok = self.live.submit(
            lambda: self.elm.request("04", timeout=2.0).ok,
            priority=P_USER_HIGH, timeout=5.0,
        )
        if ok:
            self._last_dtc_count = 0
        return ok

    def freeze_frame(self) -> FreezeFrame:
        """Read Mode 02 freeze frame for the highest-priority stored DTC.

        Returns a snapshot of common PIDs at the moment the DTC was set.
        """
        def _job() -> FreezeFrame:
            ff = FreezeFrame()
            # PID 02 of Mode 02 returns the DTC that caused the freeze.
            r = self.elm.request("020200", timeout=2.0)
            if r.ok:
                payload = b"".join(r.frames)
                idx = payload.find(b"\x42\x02")
                if idx >= 0 and len(payload) >= idx + 5:
                    word = (payload[idx + 3] << 8) | payload[idx + 4]
                    if word:
                        from .dtc import decode_dtc_word
                        ff.dtc = decode_dtc_word(word)
            for code in ("0C", "0D", "11", "0B", "05", "0F", "04", "0E", "06", "07"):
                pid = PIDS_01.get(code)
                if not pid:
                    continue
                req = f"02{pid.code}00"
                rr = self.elm.request(req, timeout=1.0)
                if not rr.ok:
                    continue
                # Strip the extra frame# byte: rebuild a synthetic Mode 01 payload.
                payload = b"".join(rr.frames)
                marker = bytes.fromhex("42" + pid.code)
                pos = payload.find(marker)
                if pos < 0:
                    continue
                body = payload[pos + 2 + 1:]  # skip "42 PP FF" (frame#)
                synth = b"\x41" + bytes.fromhex(pid.code) + body
                value = parse_response(pid, [synth])
                ff.values[pid.code] = {"name": pid.name, "unit": pid.unit, "value": value}
            return ff
        return self.live.submit(_job, priority=P_USER_HIGH, timeout=10.0)

    # ---------------- live ----------------
    def read_pid(self, pid: Pid) -> Optional[float | str]:
        """One-shot PID read. Routed through the I/O worker for serialization."""
        def _job() -> Optional[float | str]:
            r = self.elm.request(pid.request, timeout=1.2)
            if not r.ok:
                return None
            return parse_response(pid, r.frames)
        return self.live.submit(_job, priority=P_USER_NORM, timeout=3.0)

    def raw(self, cmd: str) -> Any:
        """Send a raw AT/OBD command string."""
        return self.live.submit(
            lambda: self.elm.request(cmd, timeout=2.0),
            priority=P_USER_HIGH, timeout=5.0,
        )

    # ---------------- live test ----------------
    def run_test(self, test: ActiveTest) -> bool:
        return self.live.submit(
            lambda: self.runner.run(test),
            priority=P_USER_NORM, timeout=4.0,
        )

    def stop_test(self, test: ActiveTest) -> bool:
        return self.live.submit(
            lambda: self.runner.stop(test),
            priority=P_USER_HIGH, timeout=4.0,
        )
