"""Active / live actuator tests.

ELM327 alone cannot run UDS RoutineControl, but it can issue raw OBD/UDS frames.
We expose a small catalog of tests that map to common ECU services:

  - Mode 08 (Request Control of On-board System) — generic OBD-II
  - Service 0x2F (InputOutputControlByIdentifier, UDS) — modern CAN ECUs
  - Service 0x30 (Honda HDS-style legacy)              — KWP2000 motorcycles

Each test sends a request, waits for a positive response, optionally loops while
the user keeps it engaged, then sends a "stop / return to default" frame.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .elm327 import Elm327


@dataclass(frozen=True)
class ActiveTest:
    name: str
    description: str
    start_cmd: str          # raw hex string, e.g. "2F300103"
    stop_cmd: str           # e.g. "2F300100"
    safety_note: str = ""
    requires_engine_off: bool = True


# Conservative starter catalogue. Real per-brand commands belong in profiles.
CATALOG: list[ActiveTest] = [
    ActiveTest(
        name="Fuel Pump Relay",
        description="Force fuel pump ON for 5 seconds (priming).",
        start_cmd="2F11C103", stop_cmd="2F11C100",
        safety_note="Pastikan tidak ada kebocoran bahan bakar.",
    ),
    ActiveTest(
        name="Idle Air Control (ISC)",
        description="Move idle stepper to test position.",
        start_cmd="2F11C203", stop_cmd="2F11C200",
    ),
    ActiveTest(
        name="Radiator Fan",
        description="Activate cooling fan for 10 seconds.",
        start_cmd="2F11C303", stop_cmd="2F11C300",
        requires_engine_off=False,
    ),
    ActiveTest(
        name="Injector #1 Cut",
        description="Disable injector 1 momentarily (cylinder balance test).",
        start_cmd="2F11C403", stop_cmd="2F11C400",
        safety_note="Mesin akan pincang; lepas dengan cepat.",
        requires_engine_off=False,
    ),
    ActiveTest(
        name="Ignition Coil #1",
        description="Trigger coil 1 spark test.",
        start_cmd="2F11C503", stop_cmd="2F11C500",
        safety_note="Lepas busi terlebih dahulu jika ingin uji visual.",
    ),
    ActiveTest(
        name="MIL Lamp",
        description="Turn the check-engine lamp on/off (bulb check).",
        start_cmd="2F11C603", stop_cmd="2F11C600",
        requires_engine_off=False,
    ),
]


class LiveTestRunner:
    """Run a single active test with start/stop bracketing."""

    def __init__(self, elm: Elm327):
        self.elm = elm

    def run(self, test: ActiveTest, on_response: Optional[Callable[[str], None]] = None) -> bool:
        r = self.elm.request(test.start_cmd, timeout=2.0)
        if on_response:
            on_response(f"START -> {r.raw}")
        return r.ok

    def stop(self, test: ActiveTest, on_response: Optional[Callable[[str], None]] = None) -> bool:
        r = self.elm.request(test.stop_cmd, timeout=2.0)
        if on_response:
            on_response(f"STOP  -> {r.raw}")
        return r.ok
