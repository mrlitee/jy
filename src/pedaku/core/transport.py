"""Low-level transport layers for ELM327.

Three transports are supported:

* :class:`SerialTransport`    - USB / virtual COM / pre-bound rfcomm device.
* :class:`TcpTransport`       - WiFi ELM327 (default ``192.168.0.10:35000``)
  or a Bluetooth-to-TCP bridge app on Android.
* :class:`BluetoothTransport` - native Bluetooth Classic RFCOMM (SPP) using
  Python's stdlib ``socket.AF_BLUETOOTH``. Works on Linux / rooted Termux
  with BlueZ. No extra dependency required.
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


class Transport(ABC):
    """Abstract bidirectional byte channel."""

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


class SerialTransport(Transport):
    """Serial / USB / pre-bound Bluetooth-SPP transport via pyserial."""

    def __init__(self, port: str, baudrate: int = 38400, timeout: float = 1.0):
        if serial is None:
            raise RuntimeError("pyserial not installed")
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._ser: Optional["serial.Serial"] = None

    def open(self) -> None:
        self._ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)

    def close(self) -> None:
        if self._ser and self._ser.is_open:
            self._ser.close()
        self._ser = None

    def write(self, data: bytes) -> None:
        assert self._ser is not None, "transport not open"
        self._ser.reset_input_buffer()
        self._ser.write(data)
        self._ser.flush()

    def read_until(self, terminator: bytes = b">", timeout: float = 2.0) -> bytes:
        assert self._ser is not None, "transport not open"
        end = time.monotonic() + timeout
        buf = bytearray()
        while time.monotonic() < end:
            chunk = self._ser.read(64)
            if chunk:
                buf.extend(chunk)
                if terminator in buf:
                    break
            else:
                time.sleep(0.01)
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
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def write(self, data: bytes) -> None:
        assert self._sock is not None, "transport not open"
        self._sock.sendall(data)

    def read_until(self, terminator: bytes = b">", timeout: float = 2.0) -> bytes:
        assert self._sock is not None, "transport not open"
        self._sock.settimeout(timeout)
        buf = bytearray()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                chunk = self._sock.recv(256)
            except socket.timeout:
                break
            if not chunk:
                break
            buf.extend(chunk)
            if terminator in buf:
                break
        return bytes(buf)

    @property
    def is_open(self) -> bool:
        return self._sock is not None


class TcpTransport(_SocketTransport):
    """WiFi ELM327 (default 192.168.0.10:35000) or BT-to-TCP bridge."""

    def __init__(self, host: str = "192.168.0.10", port: int = 35000, timeout: float = 2.0):
        super().__init__()
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)

    def open(self) -> None:
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
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
            raise RuntimeError(
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
            raise RuntimeError(
                f"Bluetooth connect to {self.mac} ch{self.channel} failed: {ex}. "
                "Make sure the device is paired, in range, and not bound by another app."
            ) from ex
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
