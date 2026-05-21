"""Real-time PID sampler and ECU I/O scheduler.

Why this module exists
----------------------
An ELM327 is a single half-duplex serial channel. Every command (PID read,
DTC scan, raw AT, actuator test) MUST be serialized. The old design held a
global ``threading.Lock`` and served every HTTP request with a synchronous
read, so a Mode 03 DTC scan would queue behind a slow PID poll cycle and a
"Read DTCs" click could feel like a multi-second delay.

This module replaces that with a single dedicated I/O worker per session:

* A priority queue feeds the worker. User-initiated commands jump ahead of
  background PID samples, so DTC reads / actuator tests are never starved.
* Background PID sampling runs on a per-PID schedule (fast PIDs like RPM at
  10 Hz, slow ones like battery voltage at 0.5 Hz). Latest values are
  cached and pushed to subscribers via lock-free queues, so HTTP/SSE
  consumers get fresh data without ever blocking on the ECU.
* A bounded ring buffer per PID (last ~5 minutes) feeds the live charts.

The result is "no-delay" UX: the dashboard updates as soon as a sample is
taken, and clicking "Read DTCs" pre-empts the next sample so it returns
within one ECU round-trip (~150 ms typical).

Resilience
----------
The sampler is also where we detect a *dead* link instead of just a slow
one. Every sample is graded "ok" (we got numeric data) or "fail" (the
transport raised, or every recent sample returned ``None``). Once
``_consec_fail`` exceeds :attr:`DEAD_AFTER_FAILURES` we fire
``on_link_dead`` so the owning :class:`DiagnosticSession` can mark itself
disconnected and the SSE handlers can exit cleanly.
"""
from __future__ import annotations

import heapq
import itertools
import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from .obd_pid import Pid, parse_response

log = logging.getLogger(__name__)


# Per-PID sampling period (seconds). Tuned so fast-changing signals feel
# real-time while slow signals don't waste bus bandwidth.
_DEFAULT_PERIOD: dict[str, float] = {
    "0C": 0.10,  # Engine RPM           -> 10 Hz
    "11": 0.10,  # Throttle Position    -> 10 Hz
    "0D": 0.20,  # Vehicle Speed        ->  5 Hz
    "0B": 0.20,  # MAP                  ->  5 Hz
    "04": 0.30,  # Engine Load          -> ~3 Hz
    "0E": 0.50,  # Ignition Advance     ->  2 Hz
    "14": 0.30,  # O2 Sensor V          -> ~3 Hz
    "44": 0.30,  # Lambda               -> ~3 Hz
    "06": 0.50,  # Short Fuel Trim      ->  2 Hz
    "07": 1.00,  # Long Fuel Trim       ->  1 Hz
    "05": 1.00,  # ECT
    "0F": 1.00,  # IAT
    "42": 2.00,  # Control Module V
    "33": 5.00,  # Barometric Pressure
    "5C": 2.00,  # Engine Oil Temp
}
_FALLBACK_PERIOD = 1.0

# Priority levels (lower = higher priority).
P_USER_HIGH = 0   # DTC scan, clear, raw AT typed by user
P_USER_NORM = 1   # Actuator tests
P_SAMPLE    = 5   # Background PID sampling


@dataclass(order=True)
class _Job:
    priority: int
    seq: int
    fn: Callable[[], Any] = field(compare=False)
    done: threading.Event = field(compare=False, default_factory=threading.Event)
    result: dict[str, Any] = field(compare=False, default_factory=dict)


@dataclass
class Sample:
    code: str
    name: str
    unit: str
    value: Optional[float | str]
    ts: float


class LiveSampler:
    """Owns the ECU I/O thread for one :class:`DiagnosticSession`.

    Lifecycle:

    >>> sampler = LiveSampler(elm_request_fn=session.elm.request, pids=[...])
    >>> sampler.start()
    >>> sampler.submit(lambda: session.elm.request("03"), priority=P_USER_HIGH)
    >>> sampler.stop()
    """

    HISTORY_LEN = 600  # ~5 minutes at 2 Hz, ~1 minute at 10 Hz - per PID
    # Watchdog: number of consecutive sample failures before we declare the
    # link dead. With 10 Hz fastest PID and 1Hz slowest, 25 failures is a
    # ~5-second window - long enough to ride out a single bad WiFi packet
    # but short enough to flip the UI offline before the user notices.
    DEAD_AFTER_FAILURES = 25

    def __init__(
        self,
        *,
        request_fn: Callable[[str, float], Any],
        pids: Iterable[Pid],
        on_link_dead: Optional[Callable[[str], None]] = None,
        heartbeat_fn: Optional[Callable[[], bool]] = None,
        heartbeat_interval: float = 30.0,
    ):
        self._request_fn = request_fn
        self._pids: list[Pid] = list(pids)
        # Default period per PID (seconds). ``_period`` is the ACTIVE schedule
        # used by the worker; we keep ``_default_period`` so we can revert
        # after a temporary "focus" boost.
        self._default_period: dict[str, float] = {
            p.code: _DEFAULT_PERIOD.get(p.code, _FALLBACK_PERIOD) for p in self._pids
        }
        self._period: dict[str, float] = dict(self._default_period)
        self._heap: list[tuple[float, int, Pid]] = []
        self._heap_counter = itertools.count()

        self._jobs: queue.PriorityQueue[_Job] = queue.PriorityQueue()
        self._job_counter = itertools.count()

        self._latest: dict[str, Sample] = {}
        self._history: dict[str, deque[tuple[float, Optional[float]]]] = {
            p.code: deque(maxlen=self.HISTORY_LEN) for p in self._pids
        }
        self._lock = threading.Lock()
        self._subs: set[queue.Queue[Sample]] = set()

        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread = threading.Thread(target=self._run, name="elm-io", daemon=True)
        self._started = False

        # Watchdog state -- read by the owning session.
        self._consec_fail = 0
        self._link_dead_fired = False
        self._on_link_dead = on_link_dead

        # Heartbeat: a cheap call (typically ATRV) issued every N seconds
        # so we notice a half-open link even when the user is parked on a
        # tab that doesn't actively poll PIDs.
        self._heartbeat_fn = heartbeat_fn
        self._heartbeat_interval = float(heartbeat_interval)
        self._next_heartbeat = time.monotonic() + self._heartbeat_interval

    # ---------------- lifecycle ----------------
    def start(self) -> None:
        # Guard against double-start: a Thread can only be started once, and
        # calling start() twice on the same instance raises RuntimeError.
        # DiagnosticSession creates a fresh LiveSampler per connect, but a
        # caller that retries connect() on the same session should still get
        # a clean no-op rather than a crash.
        if self._started:
            return
        now = time.monotonic()
        for p in self._pids:
            heapq.heappush(self._heap, (now, next(self._heap_counter), p))
        self._thread.start()
        self._started = True

    def stop(self, timeout: float = 3.0) -> None:
        if not self._started:
            return
        self._stop.set()
        # Wake the worker if it's idle on the job queue. The sentinel runs as
        # a no-op then the loop exits at the top because ``_stop`` is set.
        try:
            self._jobs.put_nowait(_Job(P_USER_HIGH, next(self._job_counter), lambda: None))
        except Exception:
            pass
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            log.warning("LiveSampler worker did not exit within %.1fs; "
                        "the transport will be closed regardless.", timeout)
        # Wake any blocked submit() callers with a clear error so HTTP
        # handlers don't hang on a queue that will never drain.
        self._fail_pending_jobs("session stopped")

    def pause_sampling(self) -> None:
        """Stop background PID sampling without killing the worker."""
        self._paused.set()

    def resume_sampling(self) -> None:
        self._paused.clear()

    @property
    def consecutive_failures(self) -> int:
        return self._consec_fail

    def set_focus(self, codes: Iterable[str], period_s: float) -> None:
        """Temporarily boost the sampling rate of the given PIDs.

        Used by the UI when entering the Dyno tab so that RPM, MAP, and
        engine-load all sample at the same fast rate, giving a clean
        max-effort envelope.

        ``clear_focus()`` restores the per-PID defaults.
        """
        codes_set = {c.upper() for c in codes}
        period_s = max(0.05, float(period_s))
        with self._lock:
            for code in codes_set:
                if code in self._period:
                    self._period[code] = period_s

    def clear_focus(self) -> None:
        """Restore each PID's default sampling period."""
        with self._lock:
            self._period.update(self._default_period)

    # ---------------- public API ----------------
    def submit(self, fn: Callable[[], Any], priority: int = P_USER_NORM,
               timeout: float = 5.0) -> Any:
        """Run *fn* on the I/O thread and return its result, or raise.

        High-priority jobs jump ahead of background PID sampling. Raises
        ``RuntimeError`` if the worker has already been stopped (e.g. the
        session is being torn down) so HTTP handlers fail fast instead of
        blocking on a queue that will never drain.
        """
        if self._stop.is_set() or not self._started:
            raise RuntimeError("session not active")
        job = _Job(priority=priority, seq=next(self._job_counter), fn=fn)
        self._jobs.put(job)
        if not job.done.wait(timeout):
            raise TimeoutError(f"ECU command did not complete within {timeout:.1f}s")
        if "error" in job.result:
            raise RuntimeError(job.result["error"])
        return job.result.get("value")

    def latest_snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                code: {
                    "code": s.code, "name": s.name, "unit": s.unit,
                    "value": s.value, "ts": s.ts,
                }
                for code, s in self._latest.items()
            }

    def history(self, code: str, count: int = 200) -> list[tuple[float, Optional[float]]]:
        with self._lock:
            buf = self._history.get(code)
            if not buf:
                return []
            return list(buf)[-count:]

    def all_history(self, count: int = 200) -> dict[str, list[tuple[float, Optional[float]]]]:
        with self._lock:
            return {code: list(buf)[-count:] for code, buf in self._history.items()}

    def subscribe(self) -> queue.Queue[Sample]:
        q: queue.Queue[Sample] = queue.Queue(maxsize=500)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue[Sample]) -> None:
        with self._lock:
            self._subs.discard(q)

    # ---------------- worker ----------------
    def _run(self) -> None:
        log.debug("LiveSampler worker started with %d PIDs", len(self._pids))
        while not self._stop.is_set():
            # 1. Drain any user-initiated job first (priority queue is sorted).
            try:
                job = self._jobs.get_nowait()
                self._run_job(job)
                continue
            except queue.Empty:
                pass

            # 2. Heartbeat probe whenever it's due. Safe to skip if the link
            #    has just been declared dead - we don't want to pile failures
            #    on a session that's about to be torn down.
            if (
                self._heartbeat_fn is not None
                and not self._link_dead_fired
                and time.monotonic() >= self._next_heartbeat
            ):
                self._do_heartbeat()
                self._next_heartbeat = time.monotonic() + self._heartbeat_interval

            # 3. If sampling is paused or empty, wait on the job queue.
            if self._paused.is_set() or not self._heap:
                try:
                    job = self._jobs.get(timeout=0.1)
                    self._run_job(job)
                except queue.Empty:
                    pass
                continue

            # 4. Otherwise, look at the next-due PID. If not yet due, sleep on
            #    the job queue so a user command can pre-empt us.
            next_due, _, pid = self._heap[0]
            now = time.monotonic()
            if next_due > now:
                wait = min(next_due - now, 0.1)
                try:
                    job = self._jobs.get(timeout=wait)
                    self._run_job(job)
                    continue
                except queue.Empty:
                    pass

            heapq.heappop(self._heap)
            self._sample_pid(pid)
            heapq.heappush(
                self._heap,
                (time.monotonic() + self._period[pid.code], next(self._heap_counter), pid),
            )
        log.debug("LiveSampler worker stopped")

    def _run_job(self, job: _Job) -> None:
        try:
            job.result["value"] = job.fn()
        except Exception as ex:
            job.result["error"] = str(ex)
            log.warning("ECU job raised: %s", ex)
        finally:
            job.done.set()

    def _sample_pid(self, pid: Pid) -> None:
        ok = False
        try:
            r = self._request_fn(pid.request, 1.0)
            value = parse_response(pid, r.frames) if r.ok else None
            ok = bool(r.ok)
        except Exception as ex:
            log.debug("sample %s failed: %s", pid.code, ex)
            value = None
        self._record_sample(pid, value)
        self._update_health(ok and value is not None)

    def _record_sample(self, pid: Pid, value: Optional[float | str]) -> None:
        ts = time.time()
        sample = Sample(code=pid.code, name=pid.name, unit=pid.unit, value=value, ts=ts)
        with self._lock:
            self._latest[pid.code] = sample
            self._history[pid.code].append((ts, value if isinstance(value, (int, float)) else None))
            dead = []
            for q in self._subs:
                try:
                    q.put_nowait(sample)
                except queue.Full:
                    # Slow consumer - drop oldest, keep the connection alive.
                    try:
                        q.get_nowait()
                        q.put_nowait(sample)
                    except (queue.Empty, queue.Full):
                        dead.append(q)
            for q in dead:
                self._subs.discard(q)

    def _update_health(self, ok: bool) -> None:
        if ok:
            self._consec_fail = 0
            return
        self._consec_fail += 1
        if (
            not self._link_dead_fired
            and self._consec_fail >= self.DEAD_AFTER_FAILURES
            and self._on_link_dead is not None
        ):
            self._link_dead_fired = True
            log.warning("link declared dead after %d consecutive failures",
                        self._consec_fail)
            try:
                self._on_link_dead(
                    f"no usable ECU response in last {self._consec_fail} samples",
                )
            except Exception as ex:  # pragma: no cover - defensive
                log.error("on_link_dead callback raised: %s", ex)
            # Stop sampling so we don't spin on a dead transport. The
            # session will call stop() shortly, but pausing first keeps the
            # log quiet during the teardown window.
            self.pause_sampling()

    def _do_heartbeat(self) -> None:
        try:
            ok = bool(self._heartbeat_fn())  # type: ignore[misc]
        except Exception as ex:
            log.debug("heartbeat raised: %s", ex)
            ok = False
        # A heartbeat counts toward the watchdog the same way a sample does:
        # a thread-safe check that the underlying transport actually moves
        # bytes. Crucially this also means the link is detected dead even if
        # the user only sits on tabs that never poll PIDs.
        self._update_health(ok)

    def _fail_pending_jobs(self, reason: str) -> None:
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                return
            if not job.done.is_set():
                job.result["error"] = reason
                job.done.set()


def compute_health_score(latest: dict[str, dict[str, Any]], dtc_count: int) -> int:
    """Quick heuristic 0-100 health score from live PIDs and DTC count.

    Penalises: stored DTCs, abnormal ECT/oil temp, large fuel-trim deviation,
    unusually low/high battery voltage.
    """
    score = 100.0
    score -= min(40, dtc_count * 8)

    def _val(code: str) -> Optional[float]:
        s = latest.get(code)
        if not s:
            return None
        v = s.get("value")
        return float(v) if isinstance(v, (int, float)) else None

    ect = _val("05")
    if ect is not None:
        if ect > 105:
            score -= min(20, (ect - 105) * 2)
        elif ect < 0:
            score -= 5

    oil = _val("5C")
    if oil is not None and oil > 120:
        score -= min(15, (oil - 120) * 1.5)

    sft = _val("06")
    lft = _val("07")
    if sft is not None and abs(sft) > 10:
        score -= min(10, (abs(sft) - 10) * 0.7)
    if lft is not None and abs(lft) > 10:
        score -= min(10, (abs(lft) - 10) * 0.7)

    bv = _val("42")
    if bv is not None:
        if bv < 11.5:
            score -= min(15, (11.5 - bv) * 8)
        elif bv > 15.0:
            score -= min(10, (bv - 15.0) * 5)

    return max(0, min(100, int(round(score))))
