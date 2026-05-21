"""Flask backend exposing the Diagnosa engine over a small JSON + SSE API.

The server is intentionally thin: every ECU operation is dispatched to the
session's dedicated I/O worker (see :mod:`pedaku.core.live`) so that HTTP
request handlers never block on serial latency. Live data is pushed to the
browser through Server-Sent Events on ``/api/stream`` for sub-100 ms
gauge / chart updates.

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

# Module-level session. Only the (rare) connect / disconnect operations need
# this lock - all per-command access is serialized inside the session's own
# I/O worker thread.
_session_lock = threading.Lock()
_session: Optional[DiagnosticSession] = None


def _serialize_info() -> dict[str, Any]:
    if not _session or not _session.connected:
        return {"connected": False}
    i = _session.info
    return {
        "connected": True,
        "brand": _session.brand,
        "vin": i.vin,
        "ecu_name": i.ecu_name,
        "voltage": i.voltage,
        "protocol": i.protocol,
        "adapter": i.adapter,
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
        info = _serialize_info()
        if _session and _session.connected:
            info["latest"] = _session.live.latest_snapshot()
        return jsonify(info)

    @app.get("/api/health")
    def health() -> Any:
        if not _session or not _session.connected:
            return jsonify({"connected": False, "score": 0})
        try:
            score = _session.health_score()
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
        addr = body.get("address", "")
        brand = body.get("brand", "Generic OBD-II")
        if not addr:
            return jsonify({"ok": False, "error": "address required"}), 400
        with _session_lock:
            if _session:
                try:
                    _session.disconnect()
                except Exception:
                    pass
                _session = None
            try:
                s = DiagnosticSession.from_address(addr, kind, brand)
                ok = s.connect()
            except Exception as ex:
                log.exception("connect failed")
                return jsonify({"ok": False, "error": str(ex)}), 500
            if not ok:
                return jsonify({"ok": False, "error": "ELM327 init failed (no protocol detected)"}), 500
            _session = s
        return jsonify({"ok": True, "info": _serialize_info()})

    @app.post("/api/disconnect")
    def disconnect() -> Any:
        global _session
        with _session_lock:
            if _session:
                try:
                    _session.disconnect()
                except Exception:
                    pass
                _session = None
        return jsonify({"ok": True})

    # ---------------- DTC ----------------
    @app.get("/api/dtcs")
    def dtcs() -> Any:
        if not _session or not _session.connected:
            return jsonify({"error": "not connected"}), 400
        try:
            items = _session.read_dtcs()
        except Exception as ex:
            return jsonify({"error": str(ex)}), 500
        return jsonify([
            {"code": d.code, "severity": d.severity, "brand": d.brand, "description": d.description}
            for d in items
        ])

    @app.post("/api/dtcs/clear")
    def dtcs_clear() -> Any:
        if not _session or not _session.connected:
            return jsonify({"error": "not connected"}), 400
        try:
            ok = _session.clear_dtcs()
        except Exception as ex:
            return jsonify({"ok": False, "error": str(ex)}), 500
        return jsonify({"ok": ok})

    @app.get("/api/freeze")
    def freeze() -> Any:
        if not _session or not _session.connected:
            return jsonify({"error": "not connected"}), 400
        try:
            ff = _session.freeze_frame()
        except Exception as ex:
            return jsonify({"error": str(ex)}), 500
        return jsonify({"dtc": ff.dtc, "values": ff.values})

    # ---------------- live PIDs ----------------
    @app.get("/api/pids/all")
    def pids_all() -> Any:
        if not _session or not _session.connected:
            return jsonify({"error": "not connected"}), 400
        return jsonify(_session.live.latest_snapshot())

    @app.get("/api/pid/<code>")
    def pid_one(code: str) -> Any:
        if not _session or not _session.connected:
            return jsonify({"error": "not connected"}), 400
        pid = PIDS_01.get(code.upper())
        if not pid:
            return jsonify({"error": f"unknown pid {code}"}), 404
        cached = _session.live.latest_snapshot().get(pid.code)
        if cached and (time.time() - cached["ts"]) < 1.0:
            return jsonify(cached)
        try:
            v = _session.read_pid(pid)
        except Exception as ex:
            return jsonify({"error": str(ex)}), 500
        return jsonify({
            "code": pid.code, "name": pid.name, "unit": pid.unit,
            "value": v, "ts": time.time(),
        })

    @app.get("/api/history")
    def history() -> Any:
        if not _session or not _session.connected:
            return jsonify({"error": "not connected"}), 400
        try:
            count = int(request.args.get("count", "120"))
        except ValueError:
            count = 120
        count = max(1, min(count, 600))
        data = _session.live.all_history(count=count)
        return jsonify({code: [{"ts": ts, "v": v} for ts, v in pts] for code, pts in data.items()})

    @app.post("/api/sampler/focus")
    def sampler_focus() -> Any:
        """Boost (or restore) the sampling rate of selected PIDs.

        Body: ``{"codes": ["0C", "0B", "04"], "period_ms": 100}`` to bump
        those PIDs to 10 Hz; ``{}`` (or ``{"codes": []}``) to clear.
        """
        if not _session or not _session.connected:
            return jsonify({"error": "not connected"}), 400
        body = request.get_json(force=True) or {}
        codes = body.get("codes") or []
        if not codes:
            _session.live.clear_focus()
            return jsonify({"ok": True, "focused": []})
        period_ms = float(body.get("period_ms") or 100)
        _session.live.set_focus(codes, period_ms / 1000.0)
        return jsonify({"ok": True, "focused": list(codes), "period_ms": period_ms})

    @app.get("/api/stream")
    def stream() -> Any:
        sess = _session
        if not sess or not sess.connected:
            def err_gen() -> Any:
                yield "event: error\ndata: " + json.dumps({"error": "not connected"}) + "\n\n"
            return Response(err_gen(), mimetype="text/event-stream",
                            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        def gen() -> Any:
            q = sess.live.subscribe()
            try:
                snap = sess.live.latest_snapshot()
                yield "event: snapshot\ndata: " + json.dumps(snap) + "\n\n"
                last_keepalive = time.time()
                while True:
                    if not sess.connected:
                        yield "event: end\ndata: {}\n\n"
                        return
                    try:
                        sample = q.get(timeout=2.0)
                    except queue.Empty:
                        if time.time() - last_keepalive > 12:
                            yield ": keepalive\n\n"
                            last_keepalive = time.time()
                        continue
                    payload = {
                        "code": sample.code, "name": sample.name,
                        "unit": sample.unit, "value": sample.value, "ts": sample.ts,
                    }
                    yield "data: " + json.dumps(payload) + "\n\n"
                    last_keepalive = time.time()
            finally:
                sess.live.unsubscribe(q)

        return Response(gen(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable proxy buffering (nginx)
            "Connection": "keep-alive",
        })

    # ---------------- live tests ----------------
    @app.post("/api/test/<int:idx>/start")
    def test_start(idx: int) -> Any:
        if not _session or not _session.connected:
            return jsonify({"error": "not connected"}), 400
        if not 0 <= idx < len(CATALOG):
            return jsonify({"error": "invalid index"}), 404
        try:
            ok = _session.run_test(CATALOG[idx])
        except Exception as ex:
            return jsonify({"ok": False, "error": str(ex)}), 500
        return jsonify({"ok": ok})

    @app.post("/api/test/<int:idx>/stop")
    def test_stop(idx: int) -> Any:
        if not _session or not _session.connected:
            return jsonify({"error": "not connected"}), 400
        if not 0 <= idx < len(CATALOG):
            return jsonify({"error": "invalid index"}), 404
        try:
            ok = _session.stop_test(CATALOG[idx])
        except Exception as ex:
            return jsonify({"ok": False, "error": str(ex)}), 500
        return jsonify({"ok": ok})

    # ---------------- raw ----------------
    @app.post("/api/raw")
    def raw() -> Any:
        if not _session or not _session.connected:
            return jsonify({"error": "not connected"}), 400
        body = request.get_json(force=True) or {}
        cmd = body.get("cmd", "").strip()
        if not cmd:
            return jsonify({"error": "cmd required"}), 400
        try:
            r = _session.raw(cmd)
        except Exception as ex:
            return jsonify({"error": str(ex)}), 500
        return jsonify({
            "ok": r.ok, "raw": r.raw, "error": r.error,
            "frames": [f.hex().upper() for f in r.frames],
        })

    # ---------------- report ----------------
    @app.get("/api/report")
    def report() -> Any:
        if not _session or not _session.connected:
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
            dtcs_list = _session.read_dtcs()
        except Exception:
            dtcs_list = []
        snap = _session.live.latest_snapshot()
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
            score = _session.health_score(dtc_count=len(dtcs_list))
        except Exception:
            pass

        buf = io.BytesIO()
        # Use a temporary path since reportlab's SimpleDocTemplate needs a file path.
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            try:
                write_report(Path(tmp.name), _session.info, dtcs_list, live_str, score)
                tmp.flush()
                tmp.seek(0)
                buf.write(Path(tmp.name).read_bytes())
            finally:
                Path(tmp.name).unlink(missing_ok=True)
        buf.seek(0)
        filename = f"pedaku_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        return send_file(buf, mimetype="application/pdf",
                         as_attachment=True, download_name=filename)

    return app
