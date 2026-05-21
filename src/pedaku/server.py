"""Flask backend exposing the Diagnosa engine over a small JSON + SSE API.

The server is intentionally thin: every ECU operation is dispatched to the
session's dedicated I/O worker (see :mod:`pedaku.core.live`) so that HTTP
request handlers never block on serial latency. Live data is pushed to the
browser through Server-Sent Events on ``/api/stream`` for sub-100 ms
gauge / chart updates.

Reliability features
--------------------
* Every endpoint reads ``_session`` *once* under a tiny snapshot so that a
  background watchdog flipping the session to disconnected mid-handler can
  never produce ``NoneType`` errors halfway through a JSON response.
* The SSE handler writes ``retry: 2000`` once at stream start so the
  browser's built-in EventSource auto-reconnect kicks in after 2 s instead
  of the default ~3 s, and a 5 s keepalive keeps proxies / Termux from
  hanging up the idle connection (the previous 12 s window was longer than
  most reverse-proxy idle limits).
* ``/api/state`` returns ``last_error`` so the UI can show a meaningful
  message if a connect attempt - or the watchdog - has just torn the link
  down.

Endpoints
---------
GET  /                          single-page UI
GET  /api/brands                list of supported motorcycle brands
GET  /api/tests                 catalogue of active tests
GET  /api/state                 current connection state + health score
GET  /api/bluetooth/scan        list paired Bluetooth devices (best-effort)
POST /api/connect               body: {kind, address, brand}
POST /api/disconnect
GET  /api/dtcs                  read stored/pending/permanent DTCs
POST /api/dtcs/clear
GET  /api/freeze                Mode 02 freeze frame snapshot
GET  /api/health                derived health score
GET  /api/pids/all              cached snapshot of every live PID
GET  /api/pid/<code>            cached value of one PID (one-shot if stale)
GET  /api/history               recent ring-buffer for all PIDs
GET  /api/stream                Server-Sent Events: live PID updates
POST /api/sampler/focus         boost the rate of selected PIDs (Dyno tab)
GET  /api/report                generate PDF report (downloadable)
POST /api/test/<idx>/start
POST /api/test/<idx>/stop
POST /api/raw                   body: {cmd}
"""
from __future__ import annotations

import io
import json
import logging
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from flask import Flask, Response, jsonify, render_template, request, send_file

from .core.live_test import CATALOG
from .core.obd_pid import PIDS_01, all_live_pids
from .core.protocols import PROFILES
from .core.session import DiagnosticSession
from .core.transport import discover_bluetooth_devices

log = logging.getLogger(__name__)


# SSE timing constants. Picked so that:
# - a transient blip (one missing keepalive) is invisible to the user, and
# - a real outage is detected within ~10s and the browser can reconnect.
_SSE_RETRY_MS = 2000           # tells EventSource to back off 2s on error
_SSE_KEEPALIVE_S = 5.0         # send ":\n" every 5s if no events
_SSE_QUEUE_TIMEOUT_S = 1.0     # how long subscribe queue.get blocks per spin


def _report_available() -> bool:
    """Return True if the optional ``reportlab`` dependency is importable.

    Surfaced in ``/api/state`` so the UI can disable the "Download PDF Report"
    button on installs where the user skipped the optional dependency.
    Re-checked at request time (cheap import-cache lookup) instead of
    cached, so installing reportlab in a running container is picked up
    without restarting the server.
    """
    try:
        __import__("reportlab")
        return True
    except ImportError:
        return False


# Module-level session. Only the (rare) connect / disconnect operations need
# this lock - all per-command access is serialized inside the session's own
# I/O worker thread.
_session_lock = threading.Lock()
_session: Optional[DiagnosticSession] = None


def _current_session() -> Optional[DiagnosticSession]:
    """Atomic snapshot of the module-level session pointer.

    The watchdog can set ``_session._connected = False`` between two reads,
    so we always grab a single reference up front and then check liveness
    on that reference - never on ``_session`` again. Without this, a long
    request handler could see ``_session`` flip to ``None`` mid-execution.
    """
    return _session


def _live_session() -> Optional[DiagnosticSession]:
    """Return the session iff it is currently considered alive."""
    sess = _current_session()
    if sess is None or not sess.connected:
        return None
    return sess


def _serialize_info(sess: Optional[DiagnosticSession]) -> dict[str, Any]:
    if sess is None or not sess.connected:
        return {
            "connected": False,
            "report_available": _report_available(),
            "last_error": sess.last_error if sess else "",
        }
    i = sess.info
    return {
        "connected": True,
        "brand": sess.brand,
        "vin": i.vin,
        "ecu_name": i.ecu_name,
        "voltage": i.voltage,
        "protocol": i.protocol,
        "adapter": i.adapter,
        "report_available": _report_available(),
        "last_error": sess.last_error,
    }


def create_app() -> Flask:
    base = Path(__file__).resolve().parent.parent.parent
    app = Flask(
        __name__,
        template_folder=str(base / "templates"),
        static_folder=str(base / "static"),
    )

    # ---------------- pages ----------------
    @app.get("/")
    def index() -> Any:
        return render_template("index.html")

    # ---------------- metadata ----------------
    @app.get("/api/brands")
    def brands() -> Any:
        return jsonify(list(PROFILES.keys()))

    @app.get("/api/tests")
    def tests_list() -> Any:
        return jsonify([
            {
                "index": i, "name": t.name, "description": t.description,
                "engine_off": t.requires_engine_off, "safety": t.safety_note,
            }
            for i, t in enumerate(CATALOG)
        ])

    @app.get("/api/pids/meta")
    def pids_meta() -> Any:
        return jsonify([
            {"code": p.code, "name": p.name, "unit": p.unit,
             "min": p.min_value, "max": p.max_value}
            for p in all_live_pids()
        ])

    # ---------------- state ----------------
    @app.get("/api/state")
    def state() -> Any:
        sess = _current_session()
        info = _serialize_info(sess)
        if sess is not None and sess.connected:
            info["latest"] = sess.live.latest_snapshot()
        return jsonify(info)

    @app.get("/api/health")
    def health() -> Any:
        sess = _live_session()
        if sess is None:
            return jsonify({"connected": False, "score": 0})
        try:
            score = sess.health_score()
        except Exception as ex:
            return jsonify({"error": str(ex)}), 500
        return jsonify({"connected": True, "score": score})

    @app.get("/api/bluetooth/scan")
    def bluetooth_scan() -> Any:
        devices = discover_bluetooth_devices()
        return jsonify({
            "devices": [
                {"address": d.address, "name": d.description}
                for d in devices
            ],
        })

    # ---------------- connect ----------------
    @app.post("/api/connect")
    def connect() -> Any:
        global _session
        body = request.get_json(force=True) or {}
        kind = (body.get("kind") or "serial").lower()
        addr = (body.get("address") or "").strip()
        brand = body.get("brand", "Generic OBD-II")
        if not addr:
            return jsonify({"ok": False, "error": "address required"}), 400
        with _session_lock:
            # Tear down any previous session FIRST so a stale worker thread
            # can't grab the next ECU response intended for the new session.
            old = _session
            _session = None
            if old is not None:
                try:
                    old.disconnect()
                except Exception:
                    log.debug("disconnect of previous session raised")
            try:
                s = DiagnosticSession.from_address(addr, kind, brand)
            except (ValueError, RuntimeError) as ex:
                return jsonify({"ok": False, "error": str(ex)}), 400
            except Exception as ex:
                log.exception("session construction failed")
                return jsonify({"ok": False, "error": str(ex)}), 500
            try:
                ok = s.connect()
            except Exception as ex:
                log.exception("connect failed")
                try:
                    s.disconnect()
                except Exception:
                    pass
                return jsonify({"ok": False, "error": str(ex)}), 500
            if not ok:
                err = s.last_error or "ELM327 init failed (no protocol detected)"
                try:
                    s.disconnect()
                except Exception:
                    pass
                return jsonify({"ok": False, "error": err}), 500
            _session = s
        return jsonify({"ok": True, "info": _serialize_info(s)})

    @app.post("/api/disconnect")
    def disconnect() -> Any:
        global _session
        with _session_lock:
            old = _session
            _session = None
        if old is not None:
            try:
                old.disconnect()
            except Exception:
                log.debug("disconnect raised")
        return jsonify({"ok": True})

    # ---------------- DTC ----------------
    @app.get("/api/dtcs")
    def dtcs() -> Any:
        sess = _live_session()
        if sess is None:
            return jsonify({"error": "not connected"}), 400
        try:
            items = sess.read_dtcs()
        except Exception as ex:
            return jsonify({"error": str(ex)}), 500
        return jsonify([
            {"code": d.code, "severity": d.severity, "brand": d.brand, "description": d.description}
            for d in items
        ])

    @app.post("/api/dtcs/clear")
    def dtcs_clear() -> Any:
        sess = _live_session()
        if sess is None:
            return jsonify({"error": "not connected"}), 400
        try:
            ok = sess.clear_dtcs()
        except Exception as ex:
            return jsonify({"ok": False, "error": str(ex)}), 500
        return jsonify({"ok": ok})

    @app.get("/api/freeze")
    def freeze() -> Any:
        sess = _live_session()
        if sess is None:
            return jsonify({"error": "not connected"}), 400
        try:
            ff = sess.freeze_frame()
        except Exception as ex:
            return jsonify({"error": str(ex)}), 500
        return jsonify({"dtc": ff.dtc, "values": ff.values})

    # ---------------- live PIDs ----------------
    @app.get("/api/pids/all")
    def pids_all() -> Any:
        sess = _live_session()
        if sess is None:
            return jsonify({"error": "not connected"}), 400
        return jsonify(sess.live.latest_snapshot())

    @app.get("/api/pid/<code>")
    def pid_one(code: str) -> Any:
        sess = _live_session()
        if sess is None:
            return jsonify({"error": "not connected"}), 400
        pid = PIDS_01.get(code.upper())
        if not pid:
            return jsonify({"error": f"unknown pid {code}"}), 404
        cached = sess.live.latest_snapshot().get(pid.code)
        if cached and (time.time() - cached["ts"]) < 1.0:
            return jsonify(cached)
        try:
            v = sess.read_pid(pid)
        except Exception as ex:
            return jsonify({"error": str(ex)}), 500
        return jsonify({
            "code": pid.code, "name": pid.name, "unit": pid.unit,
            "value": v, "ts": time.time(),
        })

    @app.get("/api/history")
    def history() -> Any:
        sess = _live_session()
        if sess is None:
            return jsonify({"error": "not connected"}), 400
        try:
            count = int(request.args.get("count", "120"))
        except ValueError:
            count = 120
        count = max(1, min(count, 600))
        data = sess.live.all_history(count=count)
        return jsonify({code: [{"ts": ts, "v": v} for ts, v in pts] for code, pts in data.items()})

    @app.post("/api/sampler/focus")
    def sampler_focus() -> Any:
        """Boost (or restore) the sampling rate of selected PIDs.

        Body: ``{"codes": ["0C", "0B", "04"], "period_ms": 100}`` to bump
        those PIDs to 10 Hz; ``{}`` (or ``{"codes": []}``) to clear.
        """
        sess = _live_session()
        if sess is None:
            return jsonify({"error": "not connected"}), 400
        body = request.get_json(force=True) or {}
        codes = body.get("codes") or []
        if not codes:
            sess.live.clear_focus()
            return jsonify({"ok": True, "focused": []})
        period_ms = float(body.get("period_ms") or 100)
        sess.live.set_focus(codes, period_ms / 1000.0)
        return jsonify({"ok": True, "focused": list(codes), "period_ms": period_ms})

    @app.get("/api/stream")
    def stream() -> Any:
        sess = _live_session()
        sse_headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable proxy buffering (nginx)
            "Connection": "keep-alive",
        }
        if sess is None:
            def err_gen() -> Any:
                # Even on the error path send a retry hint so the browser
                # automatically tries again once the user (re)connects.
                yield f"retry: {_SSE_RETRY_MS}\n\n"
                yield "event: error\ndata: " + json.dumps({"error": "not connected"}) + "\n\n"
                yield "event: end\ndata: {}\n\n"
            return Response(err_gen(), mimetype="text/event-stream", headers=sse_headers)

        def gen() -> Any:
            q = sess.live.subscribe()
            try:
                # Set the auto-reconnect interval on the *first* line of the
                # stream so a future reconnect honours it without us needing
                # to set it on every event.
                yield f"retry: {_SSE_RETRY_MS}\n\n"
                snap = sess.live.latest_snapshot()
                yield "event: snapshot\ndata: " + json.dumps(snap) + "\n\n"
                last_event_at = time.monotonic()
                while True:
                    if not sess.connected:
                        # Send a reason payload so the UI can show why we
                        # disconnected (watchdog timeout vs user action).
                        end_payload = json.dumps({"reason": sess.last_error or "session ended"})
                        yield "event: end\ndata: " + end_payload + "\n\n"
                        return
                    try:
                        sample = q.get(timeout=_SSE_QUEUE_TIMEOUT_S)
                    except queue.Empty:
                        # Keepalive comments are SSE-spec ignorable but they
                        # keep proxies (nginx, Cloudflare, Termux's tinyhttp)
                        # from closing the idle connection.
                        if time.monotonic() - last_event_at > _SSE_KEEPALIVE_S:
                            yield ": keepalive\n\n"
                            last_event_at = time.monotonic()
                        continue
                    payload = {
                        "code": sample.code, "name": sample.name,
                        "unit": sample.unit, "value": sample.value, "ts": sample.ts,
                    }
                    yield "data: " + json.dumps(payload) + "\n\n"
                    last_event_at = time.monotonic()
            except GeneratorExit:
                # Client disconnected: clean up the subscription so the
                # sampler doesn't keep filling a queue nobody reads. Not an
                # error, just an early return.
                log.debug("SSE client disconnected")
            finally:
                try:
                    sess.live.unsubscribe(q)
                except Exception:  # pragma: no cover - defensive
                    pass

        return Response(gen(), mimetype="text/event-stream", headers=sse_headers)

    # ---------------- live tests ----------------
    @app.post("/api/test/<int:idx>/start")
    def test_start(idx: int) -> Any:
        sess = _live_session()
        if sess is None:
            return jsonify({"error": "not connected"}), 400
        if not 0 <= idx < len(CATALOG):
            return jsonify({"error": "invalid index"}), 404
        try:
            ok = sess.run_test(CATALOG[idx])
        except Exception as ex:
            return jsonify({"ok": False, "error": str(ex)}), 500
        return jsonify({"ok": ok})

    @app.post("/api/test/<int:idx>/stop")
    def test_stop(idx: int) -> Any:
        sess = _live_session()
        if sess is None:
            return jsonify({"error": "not connected"}), 400
        if not 0 <= idx < len(CATALOG):
            return jsonify({"error": "invalid index"}), 404
        try:
            ok = sess.stop_test(CATALOG[idx])
        except Exception as ex:
            return jsonify({"ok": False, "error": str(ex)}), 500
        return jsonify({"ok": ok})

    # ---------------- raw ----------------
    @app.post("/api/raw")
    def raw() -> Any:
        sess = _live_session()
        if sess is None:
            return jsonify({"error": "not connected"}), 400
        body = request.get_json(force=True) or {}
        cmd = body.get("cmd", "").strip()
        if not cmd:
            return jsonify({"error": "cmd required"}), 400
        try:
            r = sess.raw(cmd)
        except Exception as ex:
            return jsonify({"error": str(ex)}), 500
        return jsonify({
            "ok": r.ok, "raw": r.raw, "error": r.error,
            "frames": [f.hex().upper() for f in r.frames],
        })

    # ---------------- report ----------------
    @app.get("/api/report")
    def report() -> Any:
        sess = _live_session()
        if sess is None:
            return jsonify({"error": "not connected"}), 400
        try:
            from .utils.report_pdf import write_report
        except Exception as ex:
            return jsonify({
                "error": "PDF report unavailable: install reportlab "
                         "(pip install reportlab)",
                "detail": str(ex),
            }), 500

        try:
            dtcs_list = sess.read_dtcs()
        except Exception:
            dtcs_list = []
        snap = sess.live.latest_snapshot()
        live_str: dict[str, str] = {}
        for code, e in snap.items():
            v = e.get("value")
            if v is None:
                txt = "-"
            elif isinstance(v, (int, float)):
                txt = f"{v:.2f} {e.get('unit', '')}".strip()
            else:
                txt = f"{v}"
            live_str[f"{e.get('name', code)} ({code})"] = txt
        score = 0
        try:
            score = sess.health_score(dtc_count=len(dtcs_list))
        except Exception:
            pass

        # Stream the PDF straight into a BytesIO instead of round-tripping
        # through a tempfile. reportlab's SimpleDocTemplate accepts any
        # file-like with .write(), and skipping the disk write avoids a
        # symlink-attack class of bug on shared hosts.
        buf = io.BytesIO()
        try:
            write_report(buf, sess.info, dtcs_list, live_str, score)
        except Exception as ex:
            log.exception("report generation failed")
            return jsonify({"error": f"report generation failed: {ex}"}), 500
        buf.seek(0)
        filename = f"pedaku_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        return send_file(buf, mimetype="application/pdf",
                         as_attachment=True, download_name=filename)

    # ---------------- generic error handlers ----------------
    @app.errorhandler(404)
    def _not_found(_ex: Any) -> Any:
        # JSON for /api/*, plain text otherwise so the SPA can still serve
        # arbitrary static files without us hijacking the response shape.
        if request.path.startswith("/api/"):
            return jsonify({"error": "not found"}), 404
        return ("Not Found", 404)

    @app.errorhandler(500)
    def _server_error(ex: Any) -> Any:
        log.exception("unhandled server error: %s", ex)
        if request.path.startswith("/api/"):
            return jsonify({"error": "internal server error"}), 500
        return ("Internal Server Error", 500)

    return app
