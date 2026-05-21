// charts.js — tiny canvas-based real-time chart library.
//
// Why home-grown: README promises "tanpa CDN, tanpa framework". Chart.js is
// 60 kB+ minified; the use case here (a few dozen points scrolling once per
// frame) only needs ~150 LOC of canvas drawing. The Sparkline / LineChart
// classes are designed for high-frequency push (10 Hz+) with zero allocation
// per point and HiDPI-aware rendering.

(function (global) {
  'use strict';

  function dpr() { return window.devicePixelRatio || 1; }

  function setupHiDpi(canvas) {
    const r = dpr();
    const cssW = canvas.clientWidth || canvas.width;
    const cssH = canvas.clientHeight || canvas.height;
    if (canvas.width !== Math.floor(cssW * r) || canvas.height !== Math.floor(cssH * r)) {
      canvas.width = Math.floor(cssW * r);
      canvas.height = Math.floor(cssH * r);
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(r, 0, 0, r, 0, 0);
    return { ctx, w: cssW, h: cssH };
  }

  // ------------------------- Sparkline ------------------------- //
  // Tiny background trace inside a gauge tile. Self-renders on push().
  class Sparkline {
    constructor(canvas, opts = {}) {
      this.canvas = canvas;
      this.color = opts.color || '#4ea3ff';
      this.fill  = opts.fill  || 'rgba(78,163,255,0.12)';
      this.max   = opts.max   || 90;     // ring buffer length
      this.values = new Float64Array(this.max);
      this.head = 0;
      this.count = 0;
      this.min = 0;
      this.maxV = 1;
      this._dirty = false;
      this._raf = null;
    }
    push(v) {
      if (v === null || v === undefined || Number.isNaN(v)) v = NaN;
      this.values[this.head] = v;
      this.head = (this.head + 1) % this.max;
      if (this.count < this.max) this.count++;
      this._scheduleRender();
    }
    reset() {
      this.head = 0; this.count = 0;
      this.values.fill(0);
      this._scheduleRender();
    }
    _scheduleRender() {
      if (this._raf) return;
      this._raf = requestAnimationFrame(() => { this._raf = null; this.render(); });
    }
    render() {
      const { ctx, w, h } = setupHiDpi(this.canvas);
      ctx.clearRect(0, 0, w, h);
      if (this.count < 2) return;
      // Find min/max of the visible window
      let mn = Infinity, mx = -Infinity;
      const start = (this.head - this.count + this.max) % this.max;
      for (let i = 0; i < this.count; i++) {
        const v = this.values[(start + i) % this.max];
        if (Number.isNaN(v)) continue;
        if (v < mn) mn = v;
        if (v > mx) mx = v;
      }
      if (!isFinite(mn) || !isFinite(mx)) return;
      if (mx === mn) { mx = mn + 1; }
      const pad = 2;
      const xStep = (w - pad * 2) / (this.max - 1);
      const yScale = (h - pad * 2) / (mx - mn);

      // Filled area
      ctx.beginPath();
      let started = false;
      for (let i = 0; i < this.count; i++) {
        const v = this.values[(start + i) % this.max];
        if (Number.isNaN(v)) continue;
        const x = pad + i * xStep;
        const y = h - pad - (v - mn) * yScale;
        if (!started) { ctx.moveTo(x, h - pad); ctx.lineTo(x, y); started = true; }
        else ctx.lineTo(x, y);
      }
      if (started) {
        ctx.lineTo(pad + (this.count - 1) * xStep, h - pad);
        ctx.closePath();
        ctx.fillStyle = this.fill; ctx.fill();
      }

      // Trace
      ctx.beginPath();
      started = false;
      for (let i = 0; i < this.count; i++) {
        const v = this.values[(start + i) % this.max];
        if (Number.isNaN(v)) continue;
        const x = pad + i * xStep;
        const y = h - pad - (v - mn) * yScale;
        if (!started) { ctx.moveTo(x, y); started = true; }
        else ctx.lineTo(x, y);
      }
      ctx.strokeStyle = this.color;
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }
  }

  // ------------------------- LineChart ------------------------- //
  // Multi-series scrolling chart with auto-scaling y, time-window x,
  // gridlines, legend, and tooltip on hover. Series share the y-axis;
  // values are normalized to [0,1] within their own min/max so unrelated
  // PIDs (RPM, °C, %) can be overlaid without one dwarfing the rest.
  class LineChart {
    constructor(canvas, opts = {}) {
      this.canvas = canvas;
      this.windowMs = opts.windowMs || 60_000; // 60 s default
      this.maxPoints = opts.maxPoints || 600;
      this.series = new Map();   // id -> {label, unit, color, data:[{t,v}], visible}
      this._raf = null;
      this._mouse = null;
      this.canvas.addEventListener('mousemove', (e) => {
        const r = this.canvas.getBoundingClientRect();
        this._mouse = { x: e.clientX - r.left, y: e.clientY - r.top };
        this._scheduleRender();
      });
      this.canvas.addEventListener('mouseleave', () => {
        this._mouse = null; this._scheduleRender();
      });
      window.addEventListener('resize', () => this._scheduleRender());
    }
    addSeries(id, opts = {}) {
      if (this.series.has(id)) return;
      this.series.set(id, {
        id,
        label:   opts.label   || id,
        unit:    opts.unit    || '',
        color:   opts.color   || '#4ea3ff',
        visible: opts.visible !== false,
        data:    [],
      });
    }
    setVisible(id, visible) {
      const s = this.series.get(id);
      if (!s) return;
      s.visible = !!visible;
      this._scheduleRender();
    }
    push(id, t, v) {
      const s = this.series.get(id);
      if (!s) return;
      if (v === null || v === undefined || Number.isNaN(v)) return;
      const num = +v;
      if (!isFinite(num)) return;
      s.data.push({ t, v: num });
      // Drop old points beyond window or maxPoints
      const cutoff = t - this.windowMs;
      while (s.data.length > this.maxPoints || (s.data.length && s.data[0].t < cutoff)) {
        s.data.shift();
      }
      this._scheduleRender();
    }
    setWindow(ms) {
      this.windowMs = ms;
      this._scheduleRender();
    }
    clear() {
      this.series.forEach(s => { s.data.length = 0; });
      this._scheduleRender();
    }
    _scheduleRender() {
      if (this._raf) return;
      this._raf = requestAnimationFrame(() => { this._raf = null; this.render(); });
    }
    render() {
      const { ctx, w, h } = setupHiDpi(this.canvas);
      ctx.clearRect(0, 0, w, h);
      const padL = 44, padR = 12, padT = 10, padB = 22;
      const innerW = w - padL - padR;
      const innerH = h - padT - padB;

      // Background grid
      ctx.strokeStyle = 'rgba(255,255,255,0.06)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (let i = 0; i <= 4; i++) {
        const y = padT + (innerH * i) / 4;
        ctx.moveTo(padL, y); ctx.lineTo(padL + innerW, y);
      }
      for (let i = 0; i <= 6; i++) {
        const x = padL + (innerW * i) / 6;
        ctx.moveTo(x, padT); ctx.lineTo(x, padT + innerH);
      }
      ctx.stroke();

      // Axis labels (time, normalized 0..1 % left)
      ctx.fillStyle = 'rgba(255,255,255,0.4)';
      ctx.font = '10px ui-monospace, Menlo, monospace';
      ctx.textAlign = 'right';
      for (let i = 0; i <= 4; i++) {
        const y = padT + (innerH * i) / 4;
        const pct = 100 - i * 25;
        ctx.fillText(pct + '%', padL - 4, y + 3);
      }
      ctx.textAlign = 'center';
      const sec = Math.round(this.windowMs / 1000);
      for (let i = 0; i <= 6; i++) {
        const x = padL + (innerW * i) / 6;
        const ago = Math.round(sec - (sec * i) / 6);
        ctx.fillText('-' + ago + 's', x, h - 6);
      }

      const now = Date.now();
      const xMin = now - this.windowMs;
      const visible = [...this.series.values()].filter(s => s.visible && s.data.length > 1);
      if (!visible.length) {
        ctx.fillStyle = 'rgba(255,255,255,0.3)';
        ctx.textAlign = 'center';
        ctx.font = '12px sans-serif';
        ctx.fillText('No data — connect first or pick a series', padL + innerW / 2, padT + innerH / 2);
        return;
      }

      // Per-series min/max for normalization (so different units can overlay).
      visible.forEach(s => {
        let mn = Infinity, mx = -Infinity;
        for (let i = 0; i < s.data.length; i++) {
          const v = s.data[i].v;
          if (v < mn) mn = v;
          if (v > mx) mx = v;
        }
        if (mx === mn) mx = mn + 1;
        s._mn = mn; s._mx = mx;
      });

      // Plot
      visible.forEach(s => {
        ctx.beginPath();
        let started = false;
        for (let i = 0; i < s.data.length; i++) {
          const p = s.data[i];
          if (p.t < xMin) continue;
          const x = padL + ((p.t - xMin) / this.windowMs) * innerW;
          const norm = (p.v - s._mn) / (s._mx - s._mn);
          const y = padT + innerH * (1 - norm);
          if (!started) { ctx.moveTo(x, y); started = true; }
          else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = s.color;
        ctx.lineWidth = 1.6;
        ctx.stroke();
      });

      // Tooltip / cursor
      if (this._mouse && this._mouse.x >= padL && this._mouse.x <= padL + innerW) {
        const tAtCursor = xMin + ((this._mouse.x - padL) / innerW) * this.windowMs;
        ctx.strokeStyle = 'rgba(255,255,255,0.2)';
        ctx.beginPath();
        ctx.moveTo(this._mouse.x, padT);
        ctx.lineTo(this._mouse.x, padT + innerH);
        ctx.stroke();
        // Find nearest point per series
        const items = [];
        visible.forEach(s => {
          let best = null, bestD = Infinity;
          for (let i = 0; i < s.data.length; i++) {
            const p = s.data[i];
            const d = Math.abs(p.t - tAtCursor);
            if (d < bestD) { bestD = d; best = p; }
          }
          if (best) {
            const norm = (best.v - s._mn) / (s._mx - s._mn);
            const y = padT + innerH * (1 - norm);
            ctx.beginPath();
            ctx.arc(this._mouse.x, y, 3, 0, Math.PI * 2);
            ctx.fillStyle = s.color; ctx.fill();
            items.push({ label: s.label, unit: s.unit, color: s.color, value: best.v });
          }
        });
        // Tooltip box
        if (items.length) {
          ctx.font = '11px sans-serif';
          ctx.textAlign = 'left';
          const lh = 14;
          const boxW = 150;
          const boxH = items.length * lh + 8;
          let bx = this._mouse.x + 10;
          if (bx + boxW > w - 4) bx = this._mouse.x - 10 - boxW;
          const by = padT + 4;
          ctx.fillStyle = 'rgba(20,25,35,0.92)';
          ctx.strokeStyle = 'rgba(255,255,255,0.2)';
          ctx.fillRect(bx, by, boxW, boxH);
          ctx.strokeRect(bx, by, boxW, boxH);
          items.forEach((it, i) => {
            const y = by + 6 + i * lh + 8;
            ctx.fillStyle = it.color; ctx.fillRect(bx + 6, y - 7, 8, 8);
            ctx.fillStyle = '#e8eaed';
            const val = it.value.toFixed(it.unit === 'rpm' ? 0 : 2);
            ctx.fillText(`${it.label}: ${val} ${it.unit}`, bx + 18, y);
          });
        }
      }
    }
  }

  // ------------------------- DynoChart ------------------------- //
  // Motorcycle dyno-style chart: X = RPM, dual Y axes for HP (kW or hp)
  // and Torque (Nm). Stores each sample binned by RPM and keeps the max
  // value per bin, so the rider can sweep the throttle multiple times and
  // still get a clean "max-effort" envelope curve like a real chassis dyno.
  //
  // Usage:
  //   const dc = new DynoChart(canvas, { rpmMax: 14000, binSize: 100 });
  //   dc.startRun();                     // begin a new run
  //   dc.push(rpm, hp, torque);          // each live sample
  //   dc.setLive(rpm, hp, torque);       // current operating point
  //   dc.holdRun();                      // freeze run; start new one on next push
  //   dc.reset();                        // clear all runs
  //
  // The live cursor only re-renders the cursor layer (light), so the chart
  // can run at the full 10 Hz sampler rate without dropping frames.
  class DynoChart {
    constructor(canvas, opts = {}) {
      this.canvas  = canvas;
      this.rpmMin  = opts.rpmMin  ?? 0;
      this.rpmMax  = opts.rpmMax  ?? 14000;
      this.binSize = opts.binSize ?? 100;       // RPM per bin
      this.hpColor      = opts.hpColor      || '#4ea3ff';
      this.torqueColor  = opts.torqueColor  || '#fb7185';
      this.runColors    = opts.runColors    || [
        '#fbbf24', '#34d399', '#a78bfa', '#22d3ee', '#f472b6',
      ];
      // Each "run" has hp/torque maps keyed by RPM bin index.
      this.runs = [];      // [{hpBin: Map, tqBin: Map, holding: bool}]
      this.startRun();
      this.live = null;    // {rpm, hp, torque}
      this.peakHp     = { value: 0, rpm: 0, run: -1 };
      this.peakTorque = { value: 0, rpm: 0, run: -1 };
      this._raf = null;
      this._mouse = null;
      this.canvas.addEventListener('mousemove', (e) => {
        const r = this.canvas.getBoundingClientRect();
        this._mouse = { x: e.clientX - r.left, y: e.clientY - r.top };
        this._scheduleRender();
      });
      this.canvas.addEventListener('mouseleave', () => {
        this._mouse = null; this._scheduleRender();
      });
      window.addEventListener('resize', () => this._scheduleRender());
    }

    startRun() {
      this.runs.push({ hpBin: new Map(), tqBin: new Map(), holding: false });
      if (this.runs.length > this.runColors.length + 1) {
        // Keep the chart legible by capping at runColors+1 (latest is always shown).
        this.runs.shift();
      }
      this._scheduleRender();
    }

    holdRun() {
      const run = this.runs[this.runs.length - 1];
      if (run) run.holding = true;
    }

    reset() {
      this.runs = [];
      this.peakHp     = { value: 0, rpm: 0, run: -1 };
      this.peakTorque = { value: 0, rpm: 0, run: -1 };
      this.live = null;
      this.startRun();
    }

    /** Push a new (rpm, hp, torque) sample into the active run. */
    push(rpm, hp, torque) {
      if (!isFinite(rpm) || rpm < this.rpmMin || rpm > this.rpmMax) return;
      let run = this.runs[this.runs.length - 1];
      if (!run || run.holding) { this.startRun(); run = this.runs[this.runs.length - 1]; }
      const bin = Math.floor(rpm / this.binSize) * this.binSize;
      if (isFinite(hp) && hp > 0) {
        const cur = run.hpBin.get(bin) || 0;
        if (hp > cur) run.hpBin.set(bin, hp);
        if (hp > this.peakHp.value) {
          this.peakHp = { value: hp, rpm: bin, run: this.runs.length - 1 };
        }
      }
      if (isFinite(torque) && torque > 0) {
        const cur = run.tqBin.get(bin) || 0;
        if (torque > cur) run.tqBin.set(bin, torque);
        if (torque > this.peakTorque.value) {
          this.peakTorque = { value: torque, rpm: bin, run: this.runs.length - 1 };
        }
      }
      this._scheduleRender();
    }

    setLive(rpm, hp, torque) {
      this.live = { rpm, hp, torque };
      this._scheduleRender();
    }

    _scheduleRender() {
      if (this._raf) return;
      this._raf = requestAnimationFrame(() => { this._raf = null; this.render(); });
    }

    render() {
      const { ctx, w, h } = setupHiDpi(this.canvas);
      ctx.clearRect(0, 0, w, h);
      const padL = 50, padR = 50, padT = 14, padB = 28;
      const innerW = w - padL - padR;
      const innerH = h - padT - padB;

      // Auto-scale Y axes from peaks (with headroom).
      const hpScale = Math.max(20, Math.ceil(this.peakHp.value     * 1.15 / 10) * 10);
      const tqScale = Math.max(20, Math.ceil(this.peakTorque.value * 1.15 / 10) * 10);

      // Background grid
      ctx.strokeStyle = 'rgba(255,255,255,0.06)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      const yLines = 5, xLines = 7;
      for (let i = 0; i <= yLines; i++) {
        const y = padT + (innerH * i) / yLines;
        ctx.moveTo(padL, y); ctx.lineTo(padL + innerW, y);
      }
      for (let i = 0; i <= xLines; i++) {
        const x = padL + (innerW * i) / xLines;
        ctx.moveTo(x, padT); ctx.lineTo(x, padT + innerH);
      }
      ctx.stroke();

      // Y-axis labels (left = HP, right = Torque)
      ctx.font = '10px ui-monospace, Menlo, monospace';
      ctx.textBaseline = 'middle';
      for (let i = 0; i <= yLines; i++) {
        const y = padT + (innerH * i) / yLines;
        const fracTop = 1 - i / yLines;
        ctx.fillStyle = this.hpColor;
        ctx.textAlign = 'right';
        ctx.fillText((hpScale * fracTop).toFixed(0), padL - 4, y);
        ctx.fillStyle = this.torqueColor;
        ctx.textAlign = 'left';
        ctx.fillText((tqScale * fracTop).toFixed(0), padL + innerW + 4, y);
      }
      // Y-axis titles
      ctx.fillStyle = this.hpColor;
      ctx.textAlign = 'left';
      ctx.fillText('HP', padL - 26, padT - 4);
      ctx.fillStyle = this.torqueColor;
      ctx.textAlign = 'right';
      ctx.fillText('Nm', padL + innerW + 26, padT - 4);

      // X-axis labels (RPM)
      ctx.fillStyle = 'rgba(255,255,255,0.5)';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      for (let i = 0; i <= xLines; i++) {
        const x = padL + (innerW * i) / xLines;
        const rpm = this.rpmMin + (this.rpmMax - this.rpmMin) * (i / xLines);
        ctx.fillText(rpm.toFixed(0), x, padT + innerH + 4);
      }
      ctx.fillText('RPM', padL + innerW / 2, padT + innerH + 16);

      // Plot every run.
      const xOf = (rpm) => padL + ((rpm - this.rpmMin) / (this.rpmMax - this.rpmMin)) * innerW;
      const yOfHp = (hp) => padT + innerH * (1 - hp / hpScale);
      const yOfTq = (tq) => padT + innerH * (1 - tq / tqScale);

      this.runs.forEach((run, i) => {
        const isLatest = (i === this.runs.length - 1);
        const dim = !isLatest;
        const alpha = dim ? 0.35 : 1.0;

        // Sorted bins, drawn as smoothed line.
        const drawCurve = (binMap, yFn, baseColor) => {
          if (binMap.size < 2) return;
          const pts = [...binMap.entries()]
            .sort((a, b) => a[0] - b[0])
            .map(([rpm, v]) => [xOf(rpm), yFn(v)]);
          // Fill under the curve with a soft gradient.
          const grad = ctx.createLinearGradient(0, padT, 0, padT + innerH);
          grad.addColorStop(0, this._withAlpha(baseColor, 0.25 * alpha));
          grad.addColorStop(1, this._withAlpha(baseColor, 0.0));
          ctx.beginPath();
          ctx.moveTo(pts[0][0], padT + innerH);
          for (let k = 0; k < pts.length; k++) ctx.lineTo(pts[k][0], pts[k][1]);
          ctx.lineTo(pts[pts.length - 1][0], padT + innerH);
          ctx.closePath();
          ctx.fillStyle = grad;
          ctx.fill();
          // Stroke through points (mild quadratic smoothing).
          ctx.beginPath();
          ctx.moveTo(pts[0][0], pts[0][1]);
          for (let k = 1; k < pts.length - 1; k++) {
            const mx = (pts[k][0] + pts[k + 1][0]) / 2;
            const my = (pts[k][1] + pts[k + 1][1]) / 2;
            ctx.quadraticCurveTo(pts[k][0], pts[k][1], mx, my);
          }
          ctx.lineTo(pts[pts.length - 1][0], pts[pts.length - 1][1]);
          ctx.strokeStyle = this._withAlpha(baseColor, alpha);
          ctx.lineWidth = isLatest ? 2.4 : 1.4;
          ctx.stroke();
        };
        drawCurve(run.hpBin, yOfHp, this.hpColor);
        drawCurve(run.tqBin, yOfTq, this.torqueColor);
      });

      // Peak markers (small triangle pointing down at the peak RPM).
      const drawPeak = (peak, yFn, color, label) => {
        if (peak.value <= 0) return;
        const x = xOf(peak.rpm);
        const y = yFn(peak.value);
        ctx.beginPath();
        ctx.moveTo(x, y - 8);
        ctx.lineTo(x - 5, y - 14);
        ctx.lineTo(x + 5, y - 14);
        ctx.closePath();
        ctx.fillStyle = color;
        ctx.fill();
        ctx.fillStyle = '#0f1419';
        ctx.font = 'bold 10px ui-monospace, Menlo, monospace';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        // Label box above the triangle.
        const text = `${label} ${peak.value.toFixed(1)}@${peak.rpm}`;
        const tw = ctx.measureText(text).width + 10;
        ctx.fillStyle = color;
        ctx.fillRect(x - tw / 2, y - 28, tw, 14);
        ctx.fillStyle = '#0f1419';
        ctx.fillText(text, x, y - 21);
      };
      drawPeak(this.peakHp,     yOfHp, this.hpColor,     'HP');
      drawPeak(this.peakTorque, yOfTq, this.torqueColor, 'Tq');

      // Live cursor: glowing dot at the current operating point.
      if (this.live && isFinite(this.live.rpm) && this.live.rpm > 0) {
        const x = xOf(this.live.rpm);
        ctx.strokeStyle = 'rgba(255,255,255,0.18)';
        ctx.beginPath();
        ctx.moveTo(x, padT); ctx.lineTo(x, padT + innerH);
        ctx.stroke();
        if (isFinite(this.live.hp) && this.live.hp > 0) {
          const y = yOfHp(this.live.hp);
          ctx.beginPath();
          ctx.arc(x, y, 5, 0, Math.PI * 2);
          ctx.fillStyle = this.hpColor; ctx.fill();
          ctx.beginPath();
          ctx.arc(x, y, 9, 0, Math.PI * 2);
          ctx.strokeStyle = this._withAlpha(this.hpColor, 0.5);
          ctx.lineWidth = 1; ctx.stroke();
        }
        if (isFinite(this.live.torque) && this.live.torque > 0) {
          const y = yOfTq(this.live.torque);
          ctx.beginPath();
          ctx.arc(x, y, 5, 0, Math.PI * 2);
          ctx.fillStyle = this.torqueColor; ctx.fill();
        }
        // Live readout label
        ctx.fillStyle = 'rgba(20,25,35,0.92)';
        ctx.strokeStyle = 'rgba(255,255,255,0.2)';
        const liveTxt = ` ${Math.round(this.live.rpm)} RPM `;
        ctx.font = 'bold 11px ui-monospace, Menlo, monospace';
        const lw = ctx.measureText(liveTxt).width + 4;
        ctx.fillRect(x - lw / 2, padT + innerH + 18, lw, 14);
        ctx.strokeRect(x - lw / 2, padT + innerH + 18, lw, 14);
        ctx.fillStyle = '#e8eaed';
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText(liveTxt, x, padT + innerH + 25);
      }

      // Hover crosshair: read off both curves at cursor RPM
      if (this._mouse && this._mouse.x >= padL && this._mouse.x <= padL + innerW
          && this._mouse.y >= padT && this._mouse.y <= padT + innerH) {
        const rpmAt = this.rpmMin + ((this._mouse.x - padL) / innerW) * (this.rpmMax - this.rpmMin);
        const bin = Math.floor(rpmAt / this.binSize) * this.binSize;
        const run = this.runs[this.runs.length - 1];
        const hpV = run ? run.hpBin.get(bin) : null;
        const tqV = run ? run.tqBin.get(bin) : null;
        ctx.strokeStyle = 'rgba(255,255,255,0.18)';
        ctx.beginPath();
        ctx.moveTo(this._mouse.x, padT); ctx.lineTo(this._mouse.x, padT + innerH);
        ctx.stroke();
        const lines = [`${bin} RPM`];
        if (hpV) lines.push(`HP: ${hpV.toFixed(1)}`);
        if (tqV) lines.push(`Tq: ${tqV.toFixed(1)} Nm`);
        if (lines.length > 1) {
          ctx.font = '11px ui-monospace, Menlo, monospace';
          const tw = Math.max(...lines.map(l => ctx.measureText(l).width)) + 12;
          const th = lines.length * 14 + 6;
          let bx = this._mouse.x + 8;
          if (bx + tw > w - 4) bx = this._mouse.x - 8 - tw;
          const by = this._mouse.y + 8;
          ctx.fillStyle = 'rgba(20,25,35,0.92)';
          ctx.strokeStyle = 'rgba(255,255,255,0.2)';
          ctx.fillRect(bx, by, tw, th);
          ctx.strokeRect(bx, by, tw, th);
          ctx.fillStyle = '#e8eaed';
          ctx.textAlign = 'left'; ctx.textBaseline = 'top';
          lines.forEach((l, i) => ctx.fillText(l, bx + 6, by + 4 + i * 14));
        }
      }
    }

    _withAlpha(hex, a) {
      // Accepts "#rrggbb" → "rgba(r,g,b,a)"
      if (hex[0] !== '#' || hex.length !== 7) return hex;
      const r = parseInt(hex.slice(1, 3), 16);
      const g = parseInt(hex.slice(3, 5), 16);
      const b = parseInt(hex.slice(5, 7), 16);
      return `rgba(${r},${g},${b},${a})`;
    }
  }

  global.PedakuCharts = { Sparkline, LineChart, DynoChart };
})(window);
