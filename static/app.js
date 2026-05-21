// Pedaku frontend — vanilla JS controller for the diagnostic web UI.
//
// Wire model:
//   - Server-Sent Events on /api/stream pushes one PID sample at a time
//     as the background sampler reads it. There is no client-side polling
//     loop, so the UI updates in lockstep with the ECU's actual response
//     latency (~50-150 ms per PID over a healthy ELM327 link).
//   - Each gauge owns one Sparkline (60 s window). The "Live Chart" tab
//     renders the same data on a larger LineChart with selectable series.
//   - DTC scan / clear / freeze frame / report PDF / actuator tests all
//     submit a high-priority job to the I/O worker; they pre-empt the
//     next PID sample so the user never waits for a polling cycle.
//
// Resilience model:
//   - The server emits `retry: 2000` on every SSE response, so the
//     browser's built-in EventSource auto-reconnect kicks in 2s after a
//     network blip. We trust that and DO NOT add our own setTimeout
//     reconnect on top - the previous version did, which produced
//     reconnect storms (browser + JS racing each other).
//   - A 5s /api/state poll reconciles the UI with the server's notion of
//     "connected". If the watchdog on the server has just declared the
//     link dead, we mark the UI offline without the user clicking
//     anything; conversely if the server says we're connected but our
//     EventSource is in CLOSED state, we reopen it.
//   - All API helpers retry a couple of times on transient network errors
//     so a single dropped packet doesn't surface as an "alert()".

const $  = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));

const PIDS = [
  ['0C', 'Engine RPM',        'rpm', '#fb7185'],
  ['0D', 'Vehicle Speed',     'km/h', '#34d399'],
  ['11', 'Throttle Position', '%',   '#fbbf24'],
  ['0B', 'MAP Pressure',      'kPa', '#a78bfa'],
  ['05', 'Coolant Temp',      '°C',  '#fb923c'],
  ['0F', 'Intake Air Temp',   '°C',  '#60a5fa'],
  ['0E', 'Ignition Advance',  '°',   '#f472b6'],
  ['04', 'Engine Load',       '%',   '#facc15'],
  ['06', 'Short Fuel Trim',   '%',   '#22d3ee'],
  ['07', 'Long Fuel Trim',    '%',   '#4ade80'],
  ['14', 'O2 Sensor V',       'V',   '#e879f9'],
  ['44', 'Lambda',            'λ',   '#f87171'],
  ['42', 'Control Module V',  'V',   '#38bdf8'],
  ['33', 'Barometric',        'kPa', '#94a3b8'],
  ['5C', 'Engine Oil Temp',   '°C',  '#fda4af'],
];
const PID_META = Object.fromEntries(PIDS.map(([c, n, u, col]) => [c, { name: n, unit: u, color: col }]));

// State-reconcile interval. Picked so a watchdog-triggered server-side
// disconnect surfaces in the UI within 5s without spamming /api/state.
const RECONCILE_INTERVAL_MS = 5000;
const HEALTH_INTERVAL_MS    = 5000;

const state = {
  connected: false,         // OUR view of the connection. Reconciled with /api/state.
  streamOpen: false,        // SSE readyState === OPEN
  lastEnd: null,            // {reason} from the most recent server-sent end event
  sse: null,
  sparklines: {},   // code -> Sparkline
  mainChart: null,
  selectedSeries: new Set(['0C', '11', '0B', '05']),
  lastValues: {},   // code -> last numeric value (for gauges)
  healthTimer: null,
  reconcileTimer: null,
  dyno: {
    chart: null,
    recording: false,    // pushes samples to the active run when true
    runNum: 0,           // 0 = no user-started run yet (display "—")
    cfg: { displacement: 150, peakTorque: 14, rpmMax: 11000, lossPct: 10 },
    maxEffort: 0.05,     // observed max of (MAP_kPa × load_%/100) — calibrates torque scaling
  },
};

// ---------------- helpers ----------------
async function api(method, path, body, { retries = 1, retryDelay = 400 } = {}) {
  // Tiny retry layer for transient network blips. We do NOT retry on a
  // 4xx/5xx response - that's a deliberate server-side rejection that the
  // user needs to see. Only network-level errors (TypeError from fetch)
  // and a couple of "service-temporarily-unavailable" status codes are
  // retried, so a flaky WiFi link doesn't surface as an "alert()".
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  let lastErr = null;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const r = await fetch(path, opts);
      let data = null;
      try { data = await r.json(); } catch (_) { /* ignore */ }
      if (r.ok) return data;
      // Don't retry on 4xx — that's the user's responsibility to fix.
      if (r.status < 500 && r.status !== 503) {
        throw new Error((data && data.error) || `HTTP ${r.status}`);
      }
      lastErr = new Error((data && data.error) || `HTTP ${r.status}`);
    } catch (ex) {
      lastErr = ex;
    }
    if (attempt < retries) {
      await new Promise(r => setTimeout(r, retryDelay));
    }
  }
  throw lastErr || new Error('request failed');
}

function fmtVal(code, v) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  if (typeof v !== 'number') return String(v);
  if (code === '0C' || code === '0D') return v.toFixed(0);
  if (code === '14' || code === '42' || code === '44') return v.toFixed(2);
  return v.toFixed(1);
}

// ---------------- tabs ----------------
function setupTabs() {
  $$('nav.tabs .tab').forEach(btn => {
    btn.addEventListener('click', () => activateTab(btn.dataset.tab));
  });
}
function activateTab(id) {
  $$('nav.tabs .tab').forEach(b => b.classList.toggle('active', b.dataset.tab === id));
  $$('main .panel').forEach(p => p.classList.toggle('active', p.id === `tab-${id}`));
  if (id === 'live') {
    setTimeout(() => state.mainChart && state.mainChart.render(), 0);
  } else if (id === 'dyno') {
    setTimeout(() => state.dyno.chart && state.dyno.chart.render(), 0);
    // Boost RPM/MAP/Load/TPS sampling so all four arrive at 10 Hz instead
    // of MAP=5Hz, Load=3Hz. Cleaner curves with no lag.
    if (state.connected) {
      api('POST', '/api/sampler/focus', {
        codes: ['0C', '0B', '04', '11'], period_ms: 100,
      }).catch(() => {});
    }
    state.dynoFocusActive = true;
    return;
  }
  // Restore default per-PID rates when leaving the dyno tab to avoid
  // hammering the ECU with rarely-used readings.
  if (state.connected && state.dynoFocusActive) {
    api('POST', '/api/sampler/focus', { codes: [] }).catch(() => {});
  }
  state.dynoFocusActive = false;
}

// ---------------- header status / health ----------------
function renderStatus() {
  const el = $('#status');
  if (!state.connected) {
    // Surface the watchdog reason if the server told us why we dropped.
    // Helpful when the user's screen is awake but the WiFi to the adapter
    // has just gone idle - the message tells them to wake the bike up.
    if (state.lastEnd && state.lastEnd.reason) {
      el.textContent = `offline · ${state.lastEnd.reason}`;
    } else {
      el.textContent = 'offline';
    }
    el.className = 'status offline';
    return;
  }
  const i = state.info || {};
  const v = state.lastVoltage;
  const voltStr = (typeof v === 'number' && isFinite(v))
    ? ' \u00b7 ' + v.toFixed(2) + 'V'
    : (i.voltage !== null && i.voltage !== undefined
        ? ' \u00b7 ' + (+i.voltage).toFixed(2) + 'V' : '');
  const streamMark = state.streamOpen ? '' : ' \u00b7 reconnecting…';
  el.textContent =
    `connected \u00b7 ${i.brand || '?'} \u00b7 ${i.protocol || '?'}${voltStr}${streamMark}`;
  el.className = state.streamOpen ? 'status online' : 'status warn';
}

function setStatus(connected, info) {
  state.connected = connected;
  state.info = connected ? (info || {}) : null;
  if (!connected) {
    state.lastVoltage = null;
    state.streamOpen = false;
  } else {
    // Successful (re)connect clears any stale offline reason.
    state.lastEnd = null;
  }
  renderStatus();

  $('#info-card').hidden = !connected;
  if (connected) {
    const i = state.info;
    $('#info-table').innerHTML = `
      <tr><td>Brand</td><td>${i.brand || '-'}</td></tr>
      <tr><td>Protocol</td><td>${i.protocol || '-'}</td></tr>
      <tr><td>Adapter</td><td>${i.adapter || '-'}</td></tr>
      <tr><td>Battery</td><td>${i.voltage !== null && i.voltage !== undefined ? (+i.voltage).toFixed(2) + ' V' : '-'}</td></tr>
      <tr><td>VIN</td><td>${i.vin || '-'}</td></tr>
      <tr><td>ECU</td><td>${i.ecu_name || '-'}</td></tr>`;
  }
  // Toggle the "Download PDF Report" button based on whether reportlab
  // is installed on the server. info.report_available is True/False on
  // a real connection, and may also come back from /api/state when the
  // session is offline so the user sees the warning even before connect.
  applyReportAvailability(info && info.report_available);
}

function applyReportAvailability(available) {
  const btn = $('#btn-report');
  if (!btn) return;
  // Default to enabled (assume available) when the flag isn't present so we
  // don't block users on older server builds that don't surface the flag.
  const ok = (available === undefined) ? true : !!available;
  btn.disabled = !ok;
  btn.title = ok ? 'Generate PDF report'
                 : 'PDF report unavailable: pip install reportlab on the server';
}

async function refreshHealth() {
  if (!state.connected) return;
  try {
    const h = await api('GET', '/api/health');
    const el = $('#health-score');
    if (!el) return;
    el.textContent = h.score;
    el.classList.remove('hi', 'mid', 'low');
    el.classList.add(h.score >= 80 ? 'hi' : h.score >= 50 ? 'mid' : 'low');
  } catch (e) { /* ignore */ }
}

// ---------------- SSE ----------------
//
// EventSource has built-in auto-reconnect that honours the server's
// `retry: <ms>` hint. Our server sets it to 2000 ms. We DO NOT add a
// JS-level setTimeout reconnect on top - the previous version did, which
// produced two parallel retry attempts every time. The browser handles
// the loop; we just track readyState in the UI.
function openStream() {
  closeStream();
  let sse;
  try {
    sse = new EventSource('/api/stream');
  } catch (ex) {
    console.error('EventSource ctor failed', ex);
    return;
  }
  state.sse = sse;
  setStreamDot(false); // we are CONNECTING, not OPEN

  sse.addEventListener('open', () => {
    state.streamOpen = true;
    setStreamDot(true);
    renderStatus();
  });

  sse.addEventListener('snapshot', (e) => {
    try {
      const snap = JSON.parse(e.data);
      Object.values(snap).forEach(s => applySample(s));
    } catch (ex) { console.error(ex); }
  });

  sse.onmessage = (e) => {
    try {
      const s = JSON.parse(e.data);
      applySample(s);
    } catch (ex) { console.error(ex); }
  };

  sse.addEventListener('error', (e) => {
    state.streamOpen = false;
    setStreamDot(false);
    // If the server emitted a named `error` event with a JSON payload
    // (e.g. "not connected"), capture it for the status pill. Plain
    // network errors come with no data attribute and are not actionable
    // beyond letting EventSource auto-retry.
    if (e && typeof e.data === 'string' && e.data) {
      try {
        const p = JSON.parse(e.data);
        if (p && p.error) state.lastEnd = { reason: p.error };
      } catch (_) {}
    }
    // Don't manually re-open here. EventSource will move to CONNECTING and
    // try again automatically after the server-set retry interval. We just
    // surface the "reconnecting" state in the status pill.
    renderStatus();
  });

  // Server explicitly tells us "this stream is over" when the session is
  // torn down (user clicked Disconnect, or watchdog tripped). We must
  // close() in that case - otherwise EventSource would keep reconnecting
  // and re-getting the same end event in a loop.
  sse.addEventListener('end', (e) => {
    let reason = '';
    try { reason = (JSON.parse(e.data) || {}).reason || ''; } catch (_) {}
    state.lastEnd = { reason };
    closeStream();
    // Reconcile immediately so the UI flips offline without waiting for
    // the next 5s poll.
    setStatus(false, {});
    if (state.healthTimer) { clearInterval(state.healthTimer); state.healthTimer = null; }
  });
}

function closeStream() {
  if (state.sse) {
    try { state.sse.close(); } catch (_) {}
    state.sse = null;
  }
  state.streamOpen = false;
  setStreamDot(false);
}

function setStreamDot(on) {
  const dot = $('#stream-dot');
  if (!dot) return;
  dot.classList.toggle('on', !!on);
  dot.classList.toggle('off', !on);
}

function applySample(s) {
  if (!s || !s.code) return;
  const code = s.code;
  const v = s.value;
  const t = (s.ts || (Date.now() / 1000)) * 1000;
  state.lastValues[code] = (typeof v === 'number') ? v : null;

  // Gauge value
  const gauge = $(`#g-${code}`);
  if (gauge) {
    gauge.classList.toggle('stale', v === null || v === undefined);
    gauge.querySelector('.val').textContent = fmtVal(code, v);
  }
  // Sparkline
  const sl = state.sparklines[code];
  if (sl) sl.push(typeof v === 'number' ? v : NaN);
  // Main chart
  if (state.mainChart && typeof v === 'number') {
    state.mainChart.push(code, t, v);
  }
  // Header voltage live update — driven from state, not text parsing.
  if (code === '42' && typeof v === 'number') {
    state.lastVoltage = v;
    if (state.connected) renderStatus();
  }
  // Dyno update — recomputed on every RPM sample (the fastest sampled PID,
  // ~10 Hz), so the chart cursor and HP/Tq readouts move in real time.
  if (code === '0C') updateDyno();
}

// ---------------- Dyno tab ----------------
//
// Estimasi torsi engine dari ECU saja (tanpa beban di roda):
//   effort = (MAP_kPa / 100) × (engine_load_pct / 100)   ∈ [0, ~0.95]
//   τ_engine = peak_torque_ref × effort / max_observed_effort
//   τ_wheel  = τ_engine × (1 - drivetrain_loss/100)
//   HP_wheel = τ_wheel × ω = τ_wheel × RPM × 2π / 60 / 745.7
//
// Calibration: ``maxEffort`` adapts as we see higher samples, so the curve
// shape converges quickly toward the ref-torque ceiling at full throttle.
function updateDyno() {
  const dy = state.dyno;
  if (!dy.chart) return;
  const rpm  = state.lastValues['0C'];
  const map  = state.lastValues['0B'];
  const load = state.lastValues['04'];
  const tps  = state.lastValues['11'];
  if (typeof rpm !== 'number' || rpm <= 0) return;

  // Pick the strongest available "effort" signal. MAP × load is the
  // physically grounded one; fall back to load only or TPS if some PID is
  // unsupported by the ECU (common on small bikes).
  let effort = null;
  if (typeof map === 'number' && typeof load === 'number') {
    effort = (map / 100) * (load / 100);
  } else if (typeof load === 'number') {
    effort = load / 100;
  } else if (typeof tps === 'number') {
    effort = tps / 100;
  }
  if (effort === null || effort < 0) return;

  if (effort > dy.maxEffort) dy.maxEffort = effort;
  const norm = Math.min(1, effort / dy.maxEffort);
  const tqEngine = dy.cfg.peakTorque * norm;
  const tqWheel  = tqEngine * (1 - dy.cfg.lossPct / 100);
  const hpWheel  = (tqWheel * rpm) / 7127;   // Nm·rpm → metric HP

  // Live readouts
  $('#dyno-live-rpm').textContent    = rpm.toFixed(0);
  $('#dyno-live-hp').textContent     = hpWheel.toFixed(1);
  $('#dyno-live-torque').textContent = tqWheel.toFixed(1);

  dy.chart.setLive(rpm, hpWheel, tqWheel);
  if (dy.recording) {
    // Only push above warm-up RPM so idling jitter doesn't seed the envelope.
    if (rpm >= 2500) dy.chart.push(rpm, hpWheel, tqWheel);
  }
  // Peak readouts
  if (dy.chart.peakHp.value > 0) {
    $('#dyno-peak-hp').textContent     = dy.chart.peakHp.value.toFixed(1);
    $('#dyno-peak-hp-rpm').textContent = `@ ${dy.chart.peakHp.rpm} RPM`;
  }
  if (dy.chart.peakTorque.value > 0) {
    $('#dyno-peak-tq').textContent     = dy.chart.peakTorque.value.toFixed(1);
    $('#dyno-peak-tq-rpm').textContent = `@ ${dy.chart.peakTorque.rpm} RPM`;
  }
}

function buildDyno() {
  const canvas = $('#dyno-chart');
  if (!canvas) return;
  const dy = state.dyno;
  // Read config from inputs.
  dy.cfg.displacement = +$('#dyno-displacement').value || 150;
  dy.cfg.peakTorque   = +$('#dyno-peak-torque').value  || 14;
  dy.cfg.rpmMax       = +$('#dyno-rpm-max').value      || 11000;
  dy.cfg.lossPct      = +$('#dyno-loss').value          || 10;
  dy.chart = new PedakuCharts.DynoChart(canvas, {
    rpmMin:  0,
    rpmMax:  dy.cfg.rpmMax,
    binSize: 100,
  });
}

function setupDyno() {
  const dy = state.dyno;

  // Re-build chart when any config field changes (rpmMax may have grown).
  ['#dyno-displacement', '#dyno-peak-torque', '#dyno-rpm-max', '#dyno-loss']
    .forEach(sel => $(sel).addEventListener('change', () => {
      buildDyno();
      dy.maxEffort = 0.05;       // recalibrate
      dy.recording = false;
      dy.runNum = 0;             // 0 = no user-started run yet
      $('#dyno-run-num').textContent = '—';
      $('#dyno-status').textContent = 'idle';
      $('#btn-dyno-record').textContent = 'Start Run';
    }));

  $('#btn-dyno-record').addEventListener('click', () => {
    if (!state.connected) return alert('Connect dulu');
    dy.recording = !dy.recording;
    if (dy.recording) {
      // The DynoChart constructor seeds an empty run so the chart has
      // something to draw into before the user records anything. The
      // FIRST time the user clicks Start Run we reuse that placeholder
      // (no startRun()), so the run-number on screen tracks the count
      // of runs the user actually intended to make. Subsequent clicks
      // call startRun() to begin a fresh envelope.
      if (dy.runNum > 0) dy.chart.startRun();
      dy.runNum++;
      $('#dyno-run-num').textContent = dy.runNum;
      $('#dyno-status').textContent = 'recording';
      $('#btn-dyno-record').textContent = 'Stop Run';
      $('#btn-dyno-record').classList.add('danger');
      $('#btn-dyno-record').classList.remove('primary');
    } else {
      dy.chart.holdRun();
      $('#dyno-status').textContent = 'held';
      $('#btn-dyno-record').textContent = 'Start Run';
      $('#btn-dyno-record').classList.remove('danger');
      $('#btn-dyno-record').classList.add('primary');
    }
  });

  $('#btn-dyno-hold').addEventListener('click', () => {
    if (!dy.chart) return;
    dy.chart.holdRun();
    dy.recording = false;
    $('#dyno-status').textContent = 'held';
    $('#btn-dyno-record').textContent = 'Start Run';
    $('#btn-dyno-record').classList.remove('danger');
    $('#btn-dyno-record').classList.add('primary');
  });

  $('#btn-dyno-reset').addEventListener('click', () => {
    if (!dy.chart) return;
    dy.chart.reset();
    dy.maxEffort = 0.05;
    dy.recording = false;
    dy.runNum = 0;
    $('#dyno-run-num').textContent = '—';
    $('#dyno-status').textContent = 'idle';
    $('#dyno-peak-hp').textContent = '—';
    $('#dyno-peak-tq').textContent = '—';
    $('#dyno-peak-hp-rpm').textContent = '';
    $('#dyno-peak-tq-rpm').textContent = '';
    $('#btn-dyno-record').textContent = 'Start Run';
    $('#btn-dyno-record').classList.remove('danger');
    $('#btn-dyno-record').classList.add('primary');
  });
}

// ---------------- gauges ----------------
function buildGauges() {
  const grid = $('#gauges');
  grid.innerHTML = PIDS.map(([code, name, unit, col]) => `
    <div class="gauge stale" id="g-${code}" style="--col: ${col}">
      <div class="g-head">
        <span class="name">${name}</span>
        <span class="code">${code}</span>
      </div>
      <div class="g-body">
        <span class="val">—</span>
        <span class="unit">${unit}</span>
      </div>
      <canvas class="spark" id="sp-${code}" width="200" height="32"></canvas>
    </div>`).join('');

  // Init sparklines after DOM exists.
  state.sparklines = {};
  PIDS.forEach(([code, , , col]) => {
    const el = $(`#sp-${code}`);
    if (el) state.sparklines[code] = new PedakuCharts.Sparkline(el, { color: col });
  });
}

// ---------------- live chart ----------------
function buildMainChart() {
  const canvas = $('#main-chart');
  if (!canvas) return;
  state.mainChart = new PedakuCharts.LineChart(canvas, { windowMs: 60_000 });
  PIDS.forEach(([code, name, unit, col]) => {
    state.mainChart.addSeries(code, {
      label: name, unit, color: col,
      visible: state.selectedSeries.has(code),
    });
  });

  const list = $('#chart-series');
  list.innerHTML = PIDS.map(([code, name, , col]) => `
    <label class="series-toggle" style="--col:${col}">
      <input type="checkbox" data-code="${code}" ${state.selectedSeries.has(code) ? 'checked' : ''} />
      <span class="dot"></span>${name}
    </label>`).join('');
  list.addEventListener('change', (e) => {
    const cb = e.target;
    if (!cb || cb.tagName !== 'INPUT') return;
    const code = cb.dataset.code;
    if (cb.checked) state.selectedSeries.add(code);
    else state.selectedSeries.delete(code);
    state.mainChart.setVisible(code, cb.checked);
  });

  $$('#chart-window button').forEach(b => {
    b.addEventListener('click', () => {
      $$('#chart-window button').forEach(x => x.classList.remove('active'));
      b.classList.add('active');
      state.mainChart.setWindow(parseInt(b.dataset.ms, 10));
    });
  });
}

// ---------------- brands / connect ----------------
async function loadBrands() {
  const brands = await api('GET', '/api/brands');
  $('#f-brand').innerHTML = brands.map(b => `<option value="${b}">${b}</option>`).join('');
}

const ADDRESS_PRESETS = {
  tcp:       { value: '192.168.0.10:35000', placeholder: 'host:port — e.g. 192.168.0.10:35000' },
  bluetooth: { value: '',                   placeholder: 'AA:BB:CC:DD:EE:FF (optionally @channel)' },
  serial:    { value: '/dev/rfcomm0',       placeholder: '/dev/ttyUSB0, /dev/rfcomm0, COM3 …' },
};

function applyKind(kind) {
  const preset = ADDRESS_PRESETS[kind] || ADDRESS_PRESETS.tcp;
  const addr = $('#f-address');
  // Only swap to the preset when:
  //  (a) the field is empty, or
  //  (b) the user hasn't manually edited the field this session AND the
  //      current value is the preset for ANY known kind (so a leftover
  //      tcp host:port doesn't follow you into Bluetooth mode).
  // This preserves a manually-typed address across kind changes - the old
  // logic clobbered it on every change, which was confusing once a user
  // had pasted a MAC.
  const isStalePreset = Object.values(ADDRESS_PRESETS).some(p => p.value && p.value === addr.value);
  if (!addr.dataset.touched || addr.value === '' || isStalePreset) {
    addr.value = preset.value;
    delete addr.dataset.touched;
  }
  addr.placeholder = preset.placeholder;
  addr.disabled = false;
  $('#bt-controls').hidden = (kind !== 'bluetooth');
  if (kind !== 'bluetooth') {
    $('#f-bt-devices').hidden = true;
    $('#bt-hint').textContent = '';
  }
}

async function scanBluetooth() {
  const btn = $('#btn-bt-scan');
  const sel = $('#f-bt-devices');
  const hint = $('#bt-hint');
  btn.disabled = true;
  hint.textContent = 'scanning…';
  try {
    const res = await api('GET', '/api/bluetooth/scan');
    const devs = res.devices || [];
    if (!devs.length) {
      sel.hidden = true;
      hint.textContent = 'No paired devices found. Pair via bluetoothctl first, or type the MAC manually.';
      return;
    }
    sel.innerHTML =
      '<option value="">— pilih perangkat —</option>' +
      devs.map(d => `<option value="${d.address}">${d.name || '(no name)'} — ${d.address}</option>`).join('');
    sel.hidden = false;
    hint.textContent = `${devs.length} paired device(s).`;
  } catch (ex) {
    hint.textContent = `Scan failed: ${ex.message}`;
  } finally {
    btn.disabled = false;
  }
}

function setupConnect() {
  $('#f-kind').addEventListener('change', (e) => applyKind(e.target.value));
  $('#f-address').addEventListener('input', (e) => { e.target.dataset.touched = '1'; });
  $('#btn-bt-scan').addEventListener('click', scanBluetooth);
  $('#f-bt-devices').addEventListener('change', (e) => {
    if (e.target.value) {
      $('#f-address').value = e.target.value;
      $('#f-address').dataset.touched = '1';
    }
  });

  $('#connect-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = {
      kind: $('#f-kind').value,
      address: $('#f-address').value.trim(),
      brand: $('#f-brand').value,
    };
    setStatus(false, {});
    $('#status').textContent = 'connecting…';
    const submitBtn = e.target.querySelector('button[type="submit"]');
    if (submitBtn) submitBtn.disabled = true;
    try {
      const res = await api('POST', '/api/connect', body);
      setStatus(true, res.info);
      onConnected();
      activateTab('dashboard');
    } catch (ex) {
      alert(`Connect failed: ${ex.message}`);
      setStatus(false, {});
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  });

  $('#btn-disconnect').addEventListener('click', async () => {
    closeStream();
    if (state.healthTimer) { clearInterval(state.healthTimer); state.healthTimer = null; }
    try { await api('POST', '/api/disconnect'); } catch (e) { /* ignore */ }
    setStatus(false, {});
    state.lastEnd = null;       // a user-driven disconnect is not an error
    renderStatus();
    // Reset dyno so a new bike doesn't inherit old peaks/calibration.
    if (state.dyno.chart) {
      state.dyno.chart.reset();
      state.dyno.maxEffort = 0.05;
      state.dyno.recording = false;
      state.dyno.runNum = 0;
      $('#dyno-run-num').textContent = '—';
      $('#dyno-status').textContent = 'idle';
    }
  });

  applyKind($('#f-kind').value);
}

function onConnected() {
  buildGauges();
  if (state.mainChart) state.mainChart.clear();
  openStream();
  // Seed the live chart and sparklines from the server's ring buffer so
  // a page reload (or a brief disconnect) doesn't wipe the last few minutes
  // of recorded history. Fire-and-forget: if it fails we still get future
  // samples from the SSE stream.
  seedHistory().catch(() => {});
  refreshHealth();
  if (state.healthTimer) clearInterval(state.healthTimer);
  state.healthTimer = setInterval(refreshHealth, HEALTH_INTERVAL_MS);
}

async function seedHistory() {
  if (!state.connected) return;
  // 600 = full server-side ring buffer (~5 min at 2 Hz).
  const data = await api('GET', '/api/history?count=600');
  Object.entries(data).forEach(([code, points]) => {
    if (!Array.isArray(points) || !points.length) return;
    const sl = state.sparklines[code];
    points.forEach(p => {
      const t = (p.ts || 0) * 1000;
      const v = p.v;
      if (state.mainChart && typeof v === 'number') {
        state.mainChart.push(code, t, v);
      }
      if (sl) sl.push(typeof v === 'number' ? v : NaN);
    });
    // Refresh the gauge value with the most recent point so the dashboard
    // shows a non-empty number immediately after reload, even before SSE
    // delivers the next live sample.
    const last = points[points.length - 1];
    if (last && typeof last.v === 'number') {
      state.lastValues[code] = last.v;
      const gauge = $(`#g-${code}`);
      if (gauge) {
        gauge.classList.remove('stale');
        gauge.querySelector('.val').textContent = fmtVal(code, last.v);
      }
      if (code === '42') {
        state.lastVoltage = last.v;
        renderStatus();
      }
    }
  });
}

// ---------------- state reconcile ----------------
//
// A 5s poll that compares our notion of "connected" against the server's.
// Three transitions matter:
//
//  - server-disconnected, ui-connected  -> watchdog tripped while we were
//    looking at a tab that wasn't streaming. Flip UI offline immediately.
//  - server-connected,    ui-disconnected -> some other browser tab (or a
//    refresh in flight) brought the session back up. Re-attach.
//  - server-connected,    ui-connected, sse-closed -> our SSE failed to
//    auto-reconnect (e.g. the browser saw a `event: end` and stopped). Re-
//    open it manually.
async function reconcileState() {
  let s;
  try {
    s = await api('GET', '/api/state', null, { retries: 0 });
  } catch (_) {
    return; // server itself unreachable; let the next tick try again
  }
  const serverConnected = !!(s && s.connected);
  if (serverConnected && !state.connected) {
    // Adopt the existing session (typical after a page reload).
    setStatus(true, s);
    onConnected();
    return;
  }
  if (!serverConnected && state.connected) {
    if (s && s.last_error) state.lastEnd = { reason: s.last_error };
    closeStream();
    if (state.healthTimer) { clearInterval(state.healthTimer); state.healthTimer = null; }
    setStatus(false, s || {});
    return;
  }
  if (serverConnected && state.connected) {
    // Re-open the stream if it died and the browser couldn't recover it.
    if (!state.sse || state.sse.readyState === 2 /* CLOSED */) {
      openStream();
    }
  }
}

// ---------------- DTC ----------------
function setupDtc() {
  $('#btn-dtc-read').addEventListener('click', async () => {
    if (!state.connected) return alert('Connect dulu');
    const tbody = $('#dtc-table tbody');
    const empty = $('#dtc-empty');
    tbody.innerHTML = '<tr><td colspan="4" class="hint">scanning…</td></tr>';
    empty.hidden = true;
    try {
      const items = await api('GET', '/api/dtcs');
      if (!items.length) {
        tbody.innerHTML = '';
        empty.hidden = false;
        return;
      }
      tbody.innerHTML = items.map(d => `
        <tr>
          <td><b>${d.code}</b></td>
          <td class="sev-${d.severity}">${d.severity}</td>
          <td>${d.brand}</td>
          <td>${d.description}</td>
        </tr>`).join('');
    } catch (ex) {
      tbody.innerHTML = `<tr><td colspan="4" class="hint">Error: ${ex.message}</td></tr>`;
    }
    refreshHealth();
  });

  $('#btn-dtc-clear').addEventListener('click', async () => {
    if (!state.connected) return alert('Connect dulu');
    if (!confirm('Hapus semua DTC tersimpan? Lampu MIL akan padam.')) return;
    try {
      const res = await api('POST', '/api/dtcs/clear');
      alert(res.ok ? 'Berhasil dihapus.' : 'Gagal menghapus.');
      if (res.ok) $('#btn-dtc-read').click();
    } catch (ex) { alert(ex.message); }
  });

  $('#btn-freeze').addEventListener('click', async () => {
    if (!state.connected) return alert('Connect dulu');
    const target = $('#freeze-out');
    target.textContent = 'reading freeze frame…';
    try {
      const ff = await api('GET', '/api/freeze');
      const lines = [`DTC: ${ff.dtc || '(none)'}`];
      Object.entries(ff.values || {}).forEach(([code, e]) => {
        const v = (e.value === null || e.value === undefined) ? '-' :
          (typeof e.value === 'number' ? e.value.toFixed(2) : e.value);
        lines.push(`  ${code}  ${e.name.padEnd(22)} ${String(v).padStart(8)} ${e.unit}`);
      });
      target.textContent = lines.join('\n');
    } catch (ex) {
      target.textContent = 'Error: ' + ex.message;
    }
  });
}

// ---------------- live tests ----------------
async function setupTests() {
  const list = await api('GET', '/api/tests');
  const root = $('#tests-list');
  root.innerHTML = list.map(t => `
    <div class="test-card" data-idx="${t.index}">
      <h4>${t.name}</h4>
      <span class="badge">${t.engine_off ? 'mesin mati' : 'mesin boleh hidup'}</span>
      <p>${t.description}${t.safety ? '<br><i>' + t.safety + '</i>' : ''}</p>
      <div class="actions">
        <button class="primary" data-act="start">Start</button>
        <button data-act="stop">Stop</button>
      </div>
    </div>`).join('');
  root.addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-act]');
    if (!btn) return;
    if (!state.connected) return alert('Connect dulu');
    const card = btn.closest('.test-card');
    const idx = card.dataset.idx;
    const act = btn.dataset.act;
    btn.disabled = true;
    const orig = btn.textContent;
    try {
      const res = await api('POST', `/api/test/${idx}/${act}`);
      btn.textContent = res.ok ? `${orig} ✓` : `${orig} ✗`;
      setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 1500);
    } catch (ex) {
      btn.textContent = orig;
      btn.disabled = false;
      alert(ex.message);
    }
  });
}

// ---------------- raw command ----------------
function setupRaw() {
  $('#raw-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!state.connected) return alert('Connect dulu');
    const cmd = $('#raw-cmd').value.trim();
    if (!cmd) return;
    const out = $('#raw-out');
    try {
      const res = await api('POST', '/api/raw', { cmd });
      const stamp = new Date().toLocaleTimeString();
      const block =
        `[${stamp}] > ${cmd}\n` +
        `${res.raw || '(empty)'}\n` +
        (res.frames && res.frames.length ? `frames: ${res.frames.join(', ')}\n` : '') +
        (res.error ? `error: ${res.error}\n` : '') +
        '\n';
      out.textContent = block + out.textContent;
    } catch (ex) {
      out.textContent = `[${new Date().toLocaleTimeString()}] error: ${ex.message}\n` + out.textContent;
    }
  });
}

// ---------------- report ----------------
function setupReport() {
  $('#btn-report').addEventListener('click', () => {
    if (!state.connected) return alert('Connect dulu');
    // Direct download — server returns application/pdf attachment.
    window.location.href = '/api/report';
  });
}

// ---------------- bootstrap ----------------
async function init() {
  setupTabs();
  setupConnect();
  buildGauges();
  buildMainChart();
  buildDyno();
  setupDyno();
  setupDtc();
  setupRaw();
  setupReport();
  await loadBrands();
  try { await setupTests(); } catch (e) { console.error(e); }

  try {
    const s = await api('GET', '/api/state');
    // setStatus also applies report availability; doing it both branches
    // means the "Download PDF Report" button shows a tooltip warning at
    // page load even before connect when reportlab is missing.
    if (s.connected) {
      setStatus(true, s);
      onConnected();
    } else {
      applyReportAvailability(s && s.report_available);
      // Surface a stale offline reason from a previous session.
      if (s && s.last_error) {
        state.lastEnd = { reason: s.last_error };
        renderStatus();
      }
    }
  } catch (e) { /* ignore */ }

  // Start the periodic reconcile loop. Runs forever; `state.connected`
  // is the source of truth for whether to attempt SSE reopen.
  state.reconcileTimer = setInterval(reconcileState, RECONCILE_INTERVAL_MS);

  // When the tab is hidden the browser may suspend timers / SSE. On
  // visibility-change re-run reconcileState immediately so the user sees
  // fresh data the moment they switch back to the tab.
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) reconcileState();
  });
  window.addEventListener('online',  reconcileState);
  window.addEventListener('offline', () => {
    state.streamOpen = false;
    setStreamDot(false);
    renderStatus();
  });
}

document.addEventListener('DOMContentLoaded', init);
