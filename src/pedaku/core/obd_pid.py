"""OBD-II PID encode/decode (Mode 01) + Mode 22 manufacturer PID helpers.

A PID descriptor declares: name, unit, byte length, and a decoder lambda that
turns the raw bytes (after the mode/PID echo bytes) into an engineering value.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class Pid:
    code: str            # "0C" for engine RPM, "F190" for VIN (Mode 22)
    mode: str            # "01", "09", "22", ...
    name: str
    unit: str
    nbytes: int
    decode: Callable[[bytes], float | str]
    min_value: float = 0.0
    max_value: float = 0.0  # 0 -> no fixed range

    @property
    def request(self) -> str:
        return self.mode + self.code


def _u8(b: bytes, i: int = 0) -> int:
    return b[i]


def _u16(b: bytes, i: int = 0) -> int:
    return (b[i] << 8) | b[i + 1]


# ---- Generic Mode 01 PIDs (ISO 15031-5) ----
PIDS_01: dict[str, Pid] = {
    "04": Pid("04", "01", "Engine Load",            "%",    1, lambda b: _u8(b) * 100 / 255, 0, 100),
    "05": Pid("05", "01", "Coolant Temp (ECT)",     "°C",   1, lambda b: _u8(b) - 40, -40, 215),
    "06": Pid("06", "01", "Short Fuel Trim B1",     "%",    1, lambda b: (_u8(b) - 128) * 100 / 128, -100, 99),
    "07": Pid("07", "01", "Long Fuel Trim B1",      "%",    1, lambda b: (_u8(b) - 128) * 100 / 128, -100, 99),
    "0A": Pid("0A", "01", "Fuel Pressure",          "kPa",  1, lambda b: _u8(b) * 3, 0, 765),
    "0B": Pid("0B", "01", "Intake Manifold (MAP)",  "kPa",  1, lambda b: float(_u8(b)), 0, 255),
    "0C": Pid("0C", "01", "Engine RPM",             "rpm",  2, lambda b: _u16(b) / 4, 0, 16383),
    "0D": Pid("0D", "01", "Vehicle Speed",          "km/h", 1, lambda b: float(_u8(b)), 0, 255),
    "0E": Pid("0E", "01", "Ignition Timing Adv",    "°",    1, lambda b: _u8(b) / 2 - 64, -64, 63),
    "0F": Pid("0F", "01", "Intake Air Temp (IAT)",  "°C",   1, lambda b: _u8(b) - 40, -40, 215),
    "10": Pid("10", "01", "MAF Air Flow",           "g/s",  2, lambda b: _u16(b) / 100, 0, 655),
    "11": Pid("11", "01", "Throttle Position (TPS)", "%",   1, lambda b: _u8(b) * 100 / 255, 0, 100),
    "14": Pid("14", "01", "O2 Bank1 Sensor1 V",     "V",    2, lambda b: b[0] / 200, 0, 1.275),
    "1F": Pid("1F", "01", "Run Time Since Start",   "s",    2, lambda b: float(_u16(b)), 0, 65535),
    "21": Pid("21", "01", "Distance MIL on",        "km",   2, lambda b: float(_u16(b)), 0, 65535),
    "2F": Pid("2F", "01", "Fuel Level",             "%",    1, lambda b: _u8(b) * 100 / 255, 0, 100),
    "33": Pid("33", "01", "Barometric Pressure",    "kPa",  1, lambda b: float(_u8(b)), 0, 255),
    "42": Pid("42", "01", "Control Module V",       "V",    2, lambda b: _u16(b) / 1000, 0, 65),
    "43": Pid("43", "01", "Absolute Load",          "%",    2, lambda b: _u16(b) * 100 / 255, 0, 25700),
    "44": Pid("44", "01", "Lambda (commanded)",     "λ",    2, lambda b: _u16(b) * 2 / 65535, 0, 2),
    "45": Pid("45", "01", "Relative Throttle",      "%",    1, lambda b: _u8(b) * 100 / 255, 0, 100),
    "46": Pid("46", "01", "Ambient Air Temp",       "°C",   1, lambda b: _u8(b) - 40, -40, 215),
    "5C": Pid("5C", "01", "Engine Oil Temp",        "°C",   1, lambda b: _u8(b) - 40, -40, 215),
    "5E": Pid("5E", "01", "Fuel Rate",              "L/h",  2, lambda b: _u16(b) * 0.05, 0, 3277),
}

# ---- Mode 09 (vehicle info) ----
PIDS_09: dict[str, Pid] = {
    "02": Pid("02", "09", "VIN",  "",  17, lambda b: b.decode("ascii", errors="ignore").strip("\x00"), 0, 0),
    "0A": Pid("0A", "09", "ECU Name", "", 20, lambda b: b.decode("ascii", errors="ignore").strip("\x00"), 0, 0),
}


def parse_response(pid: Pid, frames: list[bytes]) -> Optional[float | str]:
    """Strip mode/PID echo bytes and run the decoder.

    For Mode 01 the response starts with 0x41 then the 1-byte PID. For Mode 09
    it is 0x49 + PID + 1 message-counter byte. Mode 22 responses start with
    0x62 + 2-byte PID.
    """
    if not frames:
        return None
    payload = b"".join(frames)
    expected_resp = (int(pid.mode, 16) | 0x40).to_bytes(1, "big")
    idx = payload.find(expected_resp)
    if idx < 0:
        return None
    cursor = idx + 1
    pid_bytes = bytes.fromhex(pid.code)
    if not payload[cursor:].startswith(pid_bytes):
        return None
    cursor += len(pid_bytes)
    if pid.mode == "09":
        cursor += 1  # message counter
    data = payload[cursor:cursor + pid.nbytes]
    if len(data) < pid.nbytes:
        return None
    try:
        return pid.decode(data)
    except Exception:
        return None


def all_live_pids() -> list[Pid]:
    """PIDs surfaced on the live dashboard, ordered for display."""
    order = ["0C", "0D", "11", "0B", "05", "0F", "0E", "04", "06", "07", "14", "44", "42", "33", "5C"]
    return [PIDS_01[c] for c in order if c in PIDS_01]
