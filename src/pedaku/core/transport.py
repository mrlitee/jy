"""Low-level transport layers for ELM327.

Three transports are supported:

* :class:`SerialTransport`    - USB / virtual COM / pre-bound rfcomm device.
* :class:`TcpTransport`       - WiFi ELM327 (default ``192.168.0.10:35000``)
  or a Bluetooth-to-TCP bridge app on Android.
* :class:`BluetoothTransport` - native Bluetooth Classic RFCOMM (SPP) using
  Python's stdlib ``socket.AF_BLUETOOTH``. Works on Linux / rooted Termux
  with BlueZ. No extra dependency required.

All three implement a uniform :class:`Transport` interface plus a small set
of "robustness" features that make the link survive flaky WiFi / cheap
ELM327 clones:

* TCP sockets enable ``SO_KEEPALIVE`` with a ~30s probe schedule so a
  silently-disconnected adapter is detected long before the kernel's default
  2-hour timeout.
* TCP also enables ``TCP_NODELAY`` so the tiny ELM327 prompts aren't held
  up by Nagle for ~40ms each.
* Every transport drains pending input on :meth:`reopen` so a stale frame
  from before a reconnect doesn't pollute the next response.
"""
from __future__ import annotations

import contextlib
import io
import logging
import re
import shutil
import socket
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

try:
    import serial  # pyserial
except ImportError:  # pragma: no cover - allow import without pyserial during tests
    serial = None  # type: ignore

log = logging.getLogger(__name__)

_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")


def _is_valid_mac(mac: str) -> bool:
    return bool(_MAC_RE.match(mac.strip()))


@dataclass
class PortInfo:
    """Metadata about a discovered transport endpoint."""
    kind: str          # "serial" | "tcp" | "bluetooth"
    address: str       # COM3, /dev/ttyUSB0, 192.168.0.10:35000, AA:BB:CC:DD:EE:FF
    description: str = ""


class TransportError(IOError):
    """Raised when a transport read/write fails because the link is dead.

    Distinct from a transient timeout - the caller (Elm327 driver) treats
    this as "reopen and retry once" instead of "return NO DATA to user".
    """


class Transport(ABC):
    """Abstract bidirectional byte channel."""

    description: str = ""

    @abstractmethod
    def open(self) -> None: ...
    @abstractmethod
    def close(self) -> None: ...
    @abstractmethod
    def write(self, data: bytes) -> None: ...
    @abstractmethod
    def read_until(self, terminator: bytes = b">", timeout: float = 2.0) -> bytes: ...

    @property
    @abstractmethod
    def is_open(self) -> bool: ...

    def reopen(self) -> None:
        """Close + open in one call. Used by the Elm327 driver after a
        transient transport error to recover without forcing the whole
        diagnostic session to be torn down by the user."""
        try:
            self.close()
        except Exception:  # pragma: no cover - defensive
            pass
        self.open()

    def drain(self, timeout: float = 0.1) -> None:
        """Best-effort drain of any pending input bytes.

        Called before every command so a stale ``>`` prompt or partial reply
        from a previous (cancelled) command does not bleed into the next
        response. Default is a no-op; transports override.
        """


class SerialTransport(Transport):
    """Serial / USB / pre-bound Bluetooth-SPP transport via pyserial."""

    def __init__(self, port: str, baudrate: int = 38400, timeout: float = 1.0):
        if serial is None:
            raise RuntimeError("pyserial not installed")
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.description = port
        self._ser: Optional["serial.Serial"] = None

    def open(self) -> None:
        try:
            self._ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
        except Exception as ex:
            raise TransportError(f"serial open {self.port} failed: {ex}") from ex

    def close(self) -> None:
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                if ser.is_open:
                    ser.close()
            except Exception as ex:  # pragma: no cover - defensive
                log.debug("serial close error: %s", ex)

    def drain(self, timeout: float = 0.05) -> None:
        if self._ser is None or not self._ser.is_open:
            return
        try:
            self._ser.reset_input_buffer()
        except Exception as ex:
            log.debug("serial drain error: %s", ex)

    def write(self, data: bytes) -> None:
        if self._ser is None or not self._ser.is_open:
            raise TransportError("serial not open")
        try:
            self._ser.reset_input_buffer()
            self._ser.write(data)
            self._ser.flush()
        except Exception as ex:
            raise TransportError(f"serial write failed: {ex}") from ex

    def read_until(self, terminator: bytes = b">", timeout: float = 2.0) -> bytes:
        if self._ser is None or not self._ser.is_open:
            raise TransportError("serial not open")
        end = time.monotonic() + timeout
        buf = bytearray()
        try:
            while time.monotonic() < end:
                chunk = self._ser.read(64)
                if chunk:
                    buf.extend(chunk)
                    if terminator in buf:
                        break
                else:
                    time.sleep(0.01)
        except Exception as ex:
            raise TransportError(f"serial read failed: {ex}") from ex
        return bytes(buf)

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open


class _SocketTransport(Transport):
    """Shared read / write / close for any stream-socket based transport."""

    def __init__(self) -> None:
        self._sock: Optional[socket.socket] = None
        self.timeout: float = 2.0

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
                sock.close()
            except Exception as ex:  # pragma: no cover - defensive
                log.debug("socket close error: %s", ex)

    def drain(self, timeout: float = 0.05) -> None:
        sock = self._sock
        if sock is None:
            return
        try:
            sock.setblocking(False)
            try:
                while True:
                    chunk = sock.recv(256)
                    if not chunk:
                        break
            except (BlockingIOError, OSError):
                pass
            finally:
                sock.setblocking(True)
                sock.settimeout(self.timeout)
        except Exception as ex:
            log.debug("socket drain error: %s", ex)

    def write(self, data: bytes) -> None:
        sock = self._sock
        if sock is None:
            raise TransportError("socket not open")
        try:
            sock.sendall(data)
        except (OSError, socket.timeout) as ex:
            raise TransportError(f"socket write failed: {ex}") from ex

    def read_until(self, terminator: bytes = b">", timeout: float = 2.0) -> bytes:
        sock = self._sock
        if sock is None:
            raise TransportError("socket not open")
        # Total deadline-based loop; use a short per-recv timeout so we can
        # check the deadline frequently and react to the prompt as soon as
        # it arrives, instead of being blocked on a single long timeout.
        deadline = time.monotonic() + timeout
        buf = bytearray()
        peer_closed = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                sock.settimeout(min(0.5, remaining))
                chunk = sock.recv(256)
            except socket.timeout:
                continue
            except OSError as ex:
                # Peer reset / connection aborted -> bubble up so the driver
                # can reopen the transport instead of returning a corrupt
                # short read as if it were a normal "NO DATA" reply.
                if buf:
                    log.debug("socket read aborted with partial buffer: %s", ex)
                raise TransportError(f"socket read failed: {ex}") from ex
            if not chunk:
                # 0-byte recv on a stream socket = peer FINed. Stop reading
                # and tell the driver the link is dead.
                peer_closed = True
                break
            buf.extend(chunk)
            if terminator in buf:
                break
        if peer_closed and terminator not in buf:
            raise TransportError("peer closed connection")
        return bytes(buf)

    @property
    def is_open(self) -> bool:
        return self._sock is not None


def _enable_tcp_keepalive(sock: socket.socket, idle: int = 30, intvl: int = 10, cnt: int = 3) -> None:
    """Turn on aggressive TCP keepalive so a dead WiFi ELM327 is detected fast.

    Linux exposes ``TCP_KEEPIDLE/INTVL/CNT``; macOS uses ``TCP_KEEPALIVE``;
    Windows ignores everything except ``SO_KEEPALIVE``. We try each in turn
    and silently skip what isn't available - turning ``SO_KEEPALIVE`` on at
    least gives us OS-default detection (still better than nothing).
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError as ex:  # pragma: no cover - SO_KEEPALIVE always defined
        log.debug("SO_KEEPALIVE unsupported: %s", ex)
        return
    for opt_name, value in (
        ("TCP_KEEPIDLE", idle),    # Linux
        ("TCP_KEEPINTVL", intvl),  # Linux
        ("TCP_KEEPCNT", cnt),      # Linux
        ("TCP_KEEPALIVE", idle),   # macOS
    ):
        opt = getattr(socket, opt_name, None)
        if opt is None:
            continue
        try:
            sock.setsockopt(socket.IPPROTO_TCP, opt, value)
        except OSError as ex:
            log.debug("setsockopt %s=%s failed: %s", opt_name, value, ex)


class TcpTransport(_SocketTransport):
    """WiFi ELM327 (default 192.168.0.10:35000) or BT-to-TCP bridge.

    Connect timeout (``connect_timeout``) is separate from the read timeout
    so a slow DHCP / unreachable adapter doesn't make every subsequent
    ``recv`` block for the full connect window.
    """

    def __init__(
        self,
        host: str = "192.168.0.10",
        port: int = 35000,
        timeout: float = 2.0,
        connect_timeout: float = 5.0,
    ):
        super().__init__()
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self.connect_timeout = float(connect_timeout)
        self.description = f"{host}:{port}"

    def open(self) -> None:
        try:
            s = socket.create_connection(
                (self.host, self.port), timeout=self.connect_timeout,
            )
        except OSError as ex:
            raise TransportError(
                f"TCP connect to {self.host}:{self.port} failed: {ex}"
            ) from ex
        try:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:  # pragma: no cover - rare
            pass
        _enable_tcp_keepalive(s)
        s.settimeout(self.timeout)
        self._sock = s


class BluetoothTransport(_SocketTransport):
    """Native Bluetooth Classic RFCOMM (SPP) transport.

    Uses Python's stdlib ``socket.AF_BLUETOOTH`` / ``BTPROTO_RFCOMM``.
    No third-party packages are required, but a working BlueZ stack on the
    host is needed (Linux desktop or rooted Termux). The ELM327 must be
    paired beforehand (e.g. via ``bluetoothctl``).

    Address format: a six-byte MAC like ``AA:BB:CC:DD:EE:FF``. RFCOMM
    channel defaults to 1 (the SPP channel ELM327 clones almost always
    advertise) and may be overridden by appending ``@<channel>`` to the
    address - e.g. ``AA:BB:CC:DD:EE:FF@2``.
    """

    DEFAULT_CHANNEL = 1

    def __init__(self, mac: str, channel: int = DEFAULT_CHANNEL, timeout: float = 4.0):
        super().__init__()
        mac = mac.strip()
        if not _is_valid_mac(mac):
            raise ValueError(f"Invalid Bluetooth MAC address: {mac!r}")
        self.mac = mac.upper().replace("-", ":")
        self.channel = int(channel)
        self.timeout = float(timeout)
        self.description = f"{self.mac}@{self.channel}"

    @classmethod
    def from_address(cls, address: str, timeout: float = 4.0) -> "BluetoothTransport":
        """Parse ``MAC`` or ``MAC@channel`` into a :class:`BluetoothTransport`."""
        mac, _, ch = address.partition("@")
        channel = int(ch) if ch.strip() else cls.DEFAULT_CHANNEL
        return cls(mac.strip(), channel=channel, timeout=timeout)

    def open(self) -> None:
        af = getattr(socket, "AF_BLUETOOTH", None)
        proto = getattr(socket, "BTPROTO_RFCOMM", None)
        if af is None or proto is None:
            raise TransportError(
                "Native Bluetooth RFCOMM is not available on this Python build. "
                "It requires Linux/Termux with BlueZ. "
                "On Termux non-root use the 'TCP via bridge app' option instead."
            )
        s = socket.socket(af, socket.SOCK_STREAM, proto)
        s.settimeout(self.timeout)
        try:
            s.connect((self.mac, self.channel))
        except OSError as ex:
            s.close()
            raise TransportError(
                f"Bluetooth connect to {self.mac} ch{self.channel} failed: {ex}. "
                "Make sure the device is paired, in range, and not bound by another app."
            ) from ex
        # SO_KEEPALIVE on RFCOMM isn't a portable knob (BlueZ ignores it),
        # but we still set it so kernels that DO honour it can detect a
        # frozen adapter without us needing a heartbeat probe.
        with contextlib.suppress(OSError):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        s.settimeout(self.timeout)
        self._sock = s


def discover_ports() -> list[PortInfo]:
    """List likely ELM327 endpoints (serial ports + default WiFi).

    On Android / Termux, pyserial cannot enumerate serial ports and prints
    a noisy warning to stderr at import time of ``serial.tools.list_ports``.
    We therefore import it lazily and silence that single warning - the
    user is told to type the serial path manually instead.
    """
    ports: list[PortInfo] = []
    try:
        # Lazy + stderr-suppressed import: on Android / unknown POSIX
        # platforms, pyserial unconditionally writes a multi-line "don't
        # know how to enumerate ttys" message to stderr at import time.
        with contextlib.redirect_stderr(io.StringIO()):
            from serial.tools import list_ports  # type: ignore[import-not-found]
        for p in list_ports.comports():
            ports.append(PortInfo("serial", p.device, p.description or ""))
    except Exception as ex:  # pragma: no cover - environment-dependent
        log.debug("serial port enumeration unavailable: %s", ex)

    # Always offer the well-known WiFi default; user can edit.
    ports.append(PortInfo("tcp", "192.168.0.10:35000", "WiFi ELM327 (default)"))
    return ports


def discover_bluetooth_devices(timeout: float = 4.0) -> list[PortInfo]:
    """Best-effort enumeration of paired Bluetooth devices.

    Tries ``bluetoothctl devices Paired`` first (modern BlueZ), then falls
    back to ``bluetoothctl devices`` (older). Returns an empty list when
    BlueZ is unavailable, the user lacks permission, or no device is paired.
    Designed never to raise - UI layers should treat an empty result as
    "no devices found, ask user to type the MAC manually".
    """
    if shutil.which("bluetoothctl") is None:
        return []

    out: list[PortInfo] = []
    seen: set[str] = set()

    for args in (["bluetoothctl", "devices", "Paired"], ["bluetoothctl", "devices"]):
        try:
            res = subprocess.run(
                args,
                capture_output=True, text=True, timeout=timeout,
            )
        except (subprocess.SubprocessError, OSError) as ex:
            log.debug("bluetoothctl invocation failed (%s): %s", args, ex)
            continue
        if res.returncode != 0:
            continue
        for line in res.stdout.splitlines():
            # Expected format: "Device AA:BB:CC:DD:EE:FF Friendly Name"
            # Some bluetoothctl builds also print bracketed tags such as
            # "[default]" or "[trusted]" mixed into the name field; strip
            # those so the UI dropdown shows the actual device name.
            parts = line.strip().split(" ", 2)
            if len(parts) >= 2 and parts[0] == "Device" and _is_valid_mac(parts[1]):
                mac = parts[1].upper()
                if mac in seen:
                    continue
                seen.add(mac)
                raw_name = parts[2] if len(parts) > 2 else ""
                name = " ".join(
                    tok for tok in raw_name.split()
                    if not (tok.startswith("[") and tok.endswith("]"))
                ).strip()
                out.append(PortInfo("bluetooth", mac, name))
        if out:
            break  # got results from "Paired", no need to fall back
    return out
