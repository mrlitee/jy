"""End-to-end integration test for the Pedaku diagnostic engine.

This test does NOT need Flask or a physical ELM327. It plugs a synthetic
transport into :class:`DiagnosticSession`, drives every public method on
the session, and asserts that every script in the backend actually does
something useful end-to-end.

The fake transport models a CAN-protocol ELM327 with one ECU at 7E8. It
recognises the AT subset that :class:`Elm327` uses during ``initialize()``
plus the OBD modes that the rest of the engine touches:

  * Mode 01: live PIDs (RPM, ECT, MAP, TPS, battery, ...)
  * Mode 02: freeze frame (DTC and snapshotted PIDs)
  * Mode 03 / 07 / 0A: stored / pending / permanent DTCs
  * Mode 04: clear DTCs
  * Mode 09: VIN (multi-line CAN) + ECU name (multi-line CAN)
  * Service 2F: actuator tests (positive RoutineControl response)

Run as::

    python tests/test_integration.py

Exits non-zero on the first failure so it doubles as a smoke test in CI.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

# Allow running directly from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pedaku.core.session import DiagnosticSession
from pedaku.core.transport import Transport


# --------------------------------------------------------------------------- #
# Fake ELM327 transport                                                       #
# --------------------------------------------------------------------------- #
class FakeElm327Transport(Transport):
    """Deterministic in-memory ELM327 stand-in.

    Each ``write()`` parses the command and stages a canned reply. Every
    reply ends with ``\\r>`` so :class:`Elm327` can recognise the prompt.
    """

    def __init__(self) -> None:
        self._open = False
        self._buf = bytearray()
        self._cmd = bytearray()
        # Mode 02 freeze frame storage. The ECU reports DTC P0301 was the
        # cause of the freeze, with RPM=720, ECT=92C, TPS=15%, MAP=33kPa.
        self._freeze_pids: dict[str, str] = {
            "0C": "0B40",  # 720 rpm  (raw 0x0B40 / 4)
            "0D": "00",    # 0 km/h
            "11": "26",    # 15.0%   (0x26 * 100 / 255)
            "0B": "21",    # 33 kPa
            "05": "84",    # 92 C    (0x84 - 40)
            "0F": "32",    # 10 C
            "04": "33",    # ~20% load
            "0E": "80",    # 0° advance (0x80/2 - 64)
            "06": "80",    # 0%  STFT
            "07": "82",    # +1.5% LTFT
        }

    # -------- Transport API --------
    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def write(self, data: bytes) -> None:
        for byte in data:
            if byte in (0x0D, 0x0A):  # \r or \n -> command boundary
                cmd = self._cmd.decode("ascii", errors="ignore").strip().upper()
                self._cmd.clear()
                if cmd:
                    self._buf.extend(self._respond(cmd))
            else:
                self._cmd.append(byte)

    def read_until(self, terminator: bytes = b">", timeout: float = 2.0) -> bytes:
        if terminator in self._buf:
            idx = self._buf.index(terminator) + len(terminator)
            out = bytes(self._buf[:idx])
            del self._buf[:idx]
            return out
        return bytes(self._buf)

    # -------- Reply generation --------
    def _respond(self, cmd: str) -> bytes:
        """Map an ELM/OBD command to a canned reply ending in ``\\r>``."""
        # AT subset used during init / housekeeping.
        if cmd == "ATZ":
            return b"\rELM327 v1.5\r\r>"
        if cmd in ("ATE0", "ATL0", "ATS0", "ATH0", "ATAT2", "ATST64",
                   "ATIIA10", "ATIIA12", "ATIIA11"):
            return b"OK\r>"
        if cmd in ("ATSP0", "ATSP5", "ATSP6", "ATSP7"):
            return b"OK\r>"
        if cmd.startswith("ATSH"):
            return b"OK\r>"
        if cmd.startswith("ATWM"):
            return b"OK\r>"
        if cmd == "ATPC":
            return b"OK\r>"
        if cmd == "ATDPN":
            return b"6\r>"
        if cmd == "ATRV":
            return b"12.34V\r>"
        if cmd == "ATI":
            return b"ELM327 v1.5\r>"

        # OBD warm-up.
        if cmd == "0100":
            return b"4100BE3FA813\r>"

        # Mode 01 live PIDs.
        live_table = {
            "010C": "410C0B40",   # RPM 720
            "010D": "410D2D",     # 45 km/h
            "0111": "111160",     # wrong - keep below
            "010B": "410B21",     # MAP 33 kPa
            "0105": "410584",     # ECT 92 C
            "010F": "410F32",     # IAT 10 C
            "010E": "410E80",     # 0 deg advance
            "0104": "410433",
            "0106": "410680",
            "0107": "410782",
            "0114": "411480FF",
            "0144": "414480FF",
            "0142": "41423028",   # 12.328 V
            "0133": "413363",     # 99 kPa baro
            "015C": "415C5A",     # 50 C oil
            "0111": "411160",     # TPS ~37.6%
        }
        if cmd in live_table:
            return live_table[cmd].encode("ascii") + b"\r>"

        # Mode 09 multi-line CAN responses.
        if cmd == "0902":
            # VIN "1G1AB1AP3DT123456" -> 49 02 01 + ASCII(17), split into
            # 7-byte CAN frames with the ELM327 idx-prefix format.
            return (
                b"0:490201314731\r"
                b"1:41423141503344\r"
                b"2:5431323334353600\r>"
            )
        if cmd == "090A":
            # ECU name "ECM-EngineControl    " (20 chars padded with NULs)
            return (
                b"0:490A01\r"
                b"1:45434D2D456E\r"
                b"2:67696E65436F6E\r"
                b"3:74726F6C202020\r>"
            )

        # Mode 03 DTCs: P0171 (lean) + P0301 (cyl1 misfire).
        if cmd == "03":
            return b"4302017103 01\r>"
        if cmd == "07":
            return b"4700\r>"
        if cmd == "0A":
            return b"4A00\r>"
        if cmd == "04":
            return b"44\r>"

        # Mode 02 freeze-frame queries.
        if cmd == "020200":
            # 42 02 FF 03 01 -> DTC P0301 caused the freeze
            return b"4202FF0301\r>"
        if cmd.startswith("02") and len(cmd) == 6:
            # 02 PID 00 -> 42 PID FF <data>
            pid = cmd[2:4]
            data = self._freeze_pids.get(pid)
            if data is None:
                return b"NO DATA\r>"
            return f"42{pid}FF{data}".encode("ascii") + b"\r>"

        # Service 2F (RoutineControl) actuator tests: positive response.
        if cmd.startswith("2F"):
            return b"6F" + cmd[2:].encode("ascii") + b"00\r>"

        # Unknown -> ELM error.
        return b"?\r>"


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #
class IntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = FakeElm327Transport()
        self.session = DiagnosticSession(transport=self.transport, brand="Generic OBD-II")

    def tearDown(self) -> None:
        try:
            self.session.disconnect()
        except Exception:
            pass

    def test_full_flow(self) -> None:
        # 1. connect() runs ATZ/ATE0/.../ATSP0/0100/ATDPN, sets adapter info,
        #    then reads VIN + ECU name through the freshly-fixed multi-line
        #    CAN parser. Asserts the parser actually reassembled the frames.
        ok = self.session.connect()
        self.assertTrue(ok, "connect failed")
        self.assertTrue(self.session.connected)
        self.assertEqual(self.session.info.protocol, "6")
        self.assertEqual(self.session.info.voltage, 12.34)
        self.assertIn("ELM327", self.session.info.adapter)
        self.assertEqual(self.session.info.vin, "1G1AB1AP3DT123456")
        self.assertTrue(self.session.info.ecu_name.startswith("ECM-"))

        # 2. The LiveSampler thread is now running; wait briefly for at
        #    least RPM, ECT, MAP, and battery to be sampled.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            snap = self.session.live.latest_snapshot()
            if all(c in snap for c in ("0C", "05", "0B", "42")):
                break
            time.sleep(0.05)
        snap = self.session.live.latest_snapshot()
        self.assertAlmostEqual(snap["0C"]["value"], 720, delta=1)
        self.assertAlmostEqual(snap["05"]["value"], 92, delta=1)
        self.assertAlmostEqual(snap["0B"]["value"], 33, delta=1)
        self.assertAlmostEqual(snap["42"]["value"], 12.328, delta=0.01)

        # 3. read_dtcs() pre-empts the sampler thanks to P_USER_HIGH; with
        #    the fixed parser P0171 + P0301 are decoded and looked up in
        #    the brand-specific JSON description tables.
        dtcs = self.session.read_dtcs()
        codes = [d.code for d in dtcs]
        self.assertIn("P0171", codes)
        self.assertIn("P0301", codes)
        # Description was looked up successfully (i.e. db.describe ran).
        for d in dtcs:
            self.assertTrue(d.description, f"missing description for {d.code}")

        # 4. health_score() factors the cached DTC count.
        score = self.session.health_score()
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)

        # 5. clear_dtcs() routes Mode 04 through the worker.
        self.assertTrue(self.session.clear_dtcs())

        # 6. freeze_frame() queries Mode 02 PID 02 + several PIDs and
        #    rebuilds synthetic Mode 01 payloads for the decoder.
        ff = self.session.freeze_frame()
        self.assertEqual(ff.dtc, "P0301")
        self.assertIn("0C", ff.values)
        self.assertAlmostEqual(ff.values["0C"]["value"], 720, delta=1)
        self.assertIn("05", ff.values)
        self.assertAlmostEqual(ff.values["05"]["value"], 92, delta=1)

        # 7. read_pid() does a one-shot read through the worker.
        from pedaku.core.obd_pid import PIDS_01
        rpm = self.session.read_pid(PIDS_01["0C"])
        self.assertAlmostEqual(rpm, 720, delta=1)

        # 8. raw() returns an ElmResponse with .ok and decoded frames.
        r = self.session.raw("ATRV")
        self.assertTrue(r.ok)
        self.assertIn("12.34", r.raw)

        # 9. set_focus()/clear_focus() are accepted and don't crash the
        #    sampler. We just want to prove the wires are connected.
        self.session.live.set_focus(["0C", "0B", "04", "11"], 0.1)
        self.session.live.clear_focus()

        # 10. Actuator tests: the runner sends Service 2F start/stop and
        #     reads the positive RoutineControl response.
        from pedaku.core.live_test import CATALOG
        self.assertTrue(self.session.run_test(CATALOG[0]))
        self.assertTrue(self.session.stop_test(CATALOG[0]))

        # 11. disconnect() cleanly stops the worker; subsequent submit()
        #     fails fast (proven separately, this just confirms no hang).
        self.session.disconnect()
        self.assertFalse(self.session.connected)

    def test_history_endpoint_data_shape(self) -> None:
        """The ring buffer fed to /api/history is per-PID (ts, value) tuples."""
        self.assertTrue(self.session.connect())
        # Wait for at least one sample of RPM.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if self.session.live.history("0C"):
                break
            time.sleep(0.05)
        hist = self.session.live.all_history(count=50)
        self.assertIn("0C", hist)
        self.assertGreater(len(hist["0C"]), 0)
        ts, v = hist["0C"][-1]
        self.assertIsInstance(ts, float)
        self.assertAlmostEqual(v, 720, delta=1)

    def test_report_availability_flag(self) -> None:
        """server._report_available() reflects the current import state."""
        try:
            from pedaku import server
        except ImportError as ex:
            self.skipTest(f"flask not installed in this environment: {ex}")
        # The function returns True iff reportlab is importable; it shouldn't
        # raise on either branch.
        self.assertIsInstance(server._report_available(), bool)


if __name__ == "__main__":
    unittest.main()
