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

const state = {
  connected: false,
  sse: null,
  sparklines: {},   // code -> Sparkline
  mainChart: null,
  selectedSeries: new Set(['0C', '11', '0B', '05']),
  lastValues: {},   // code -> last numeric value (for gauges)
  healthTimer: null,
  dyno: {
    chart: null,
    recording: false,    // pushes samples to the active run when true
    runNum: 1,
    cfg: { displacement: 150, peakTorque: 14, rpmMax: 11000, lossPct: 10 },
    maxEffort: 0.05,     // observed max of (MAP_kPa × load_%/100) — calibrates torque scaling
  },
};

// ---------------- helpers ----------------
async function api(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (e) { /* ignore */ }
  if (!r.ok) throw new Error((data && data.error) || `HTTP ${r.status}`);
  return data;
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
  } else {
    // Restore default per-PID rates when leaving the dyno tab to avoid
    // hammering the ECU with rarely-used readings.
    if (state.connected && state.dynoFocusActive) {
      api('POST', '/api/sampler/focus', { codes: [] }).catch(() => {});
      state.dynoFocusActive = false;
    }
  }
  state.dynoFocusActive = (id === 'dyno');
}

// ---------------- header status / health ----------------
function setStatus(connected, info) {
  state.connected = connected;
  const el = $('#status');
  if (connected) {
    const v = info.voltage !== null && info.voltage !== undefined
      ? ' · ' + (+info.voltage).toFixed(2) + 'V' : '';
    el.textContent = `connected · ${info.brand} · ${info.protocol || '?'}${v}`;
    el.className = 'status online';
  } else {
    el.textContent = 'offline';
    el.className = 'status offline';
  }

  $('#info-card').hidden = !connected;
  if (connected) {
    $('#info-table').innerHTML = `
      <tr><td>Brand</td><td>${info.brand || '-'}</td></tr>
      <tr><td>Protocol</td><td>${info.protocol || '-'}</td></tr>
      <tr><td>Adapter</td><td>${info.adapter || '-'}</td></tr>
      <tr><td>Battery</td><td>${info.voltage !== null && info.voltage !== undefined ? (+info.voltage).toFixed(2) + ' V' : '-'}</td></tr>
      <tr><td>VIN</td><td>${info.vin || '-'}</td></tr>
      <tr><td>ECU</td><td>${info.ecu_name || '-'}</td></tr>`;
  }
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
function openStream() {
  closeStream();
  const sse = new EventSource('/api/stream');
  state.sse = sse;
  $('#stream-dot').classList.remove('off');
  $('#stream-dot').classList.add('on');

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
  sse.addEventListener('error', () => {
    $('#stream-dot').classList.remove('on');
    $('#stream-dot').classList.add('off');
    if (state.connected) {
      // Auto-reconnect after a short backoff.
      setTimeout(() => { if (state.connected) openStream(); }, 1500);
    }
  });
  sse.addEventListener('end', () => closeStream());
}
function closeStream() {
  if (state.sse) { state.sse.close(); state.sse = null; }
  $('#stream-dot').classList.remove('on');
  $('#stream-dot').classList.add('off');
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
  // Header voltage live update
  if (code === '42' && typeof v === 'number') {
    const el = $('#status');
    if (state.connected && el) {
      const txt = el.textContent.split(' · ');
      txt[3] = v.toFixed(2) + 'V';
      el.textContent = txt.slice(0, 3).join(' · ') + ' · ' + v.toFixed(2) + 'V';
    }
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
      dy.runNum = 1;
      $('#dyno-run-num').textContent = dy.runNum;
      $('#dyno-status').textContent = 'idle';
      $('#btn-dyno-record').textContent = 'Start Run';
    }));

  $('#btn-dyno-record').addEventListener('click', () => {
    if (!state.connected) return alert('Connect dulu');
    dy.recording = !dy.recording;
    if (dy.recording) {
      dy.chart.startRun();
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
    dy.runNum = 1;
    $('#dyno-run-num').textContent = dy.runNum;
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
  if (!addr.dataset.touched || addr.value === '') addr.value = preset.value;
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
    try {
      const res = await api('POST', '/api/connect', body);
      setStatus(true, res.info);
      onConnected();
      activateTab('dashboard');
    } catch (ex) {
      alert(`Connect failed: ${ex.message}`);
      setStatus(false, {});
    }
  });

  $('#btn-disconnect').addEventListener('click', async () => {
    closeStream();
    if (state.healthTimer) { clearInterval(state.healthTimer); state.healthTimer = null; }
    try { await api('POST', '/api/disconnect'); } catch (e) { /* ignore */ }
    setStatus(false, {});
    // Reset dyno so a new bike doesn't inherit old peaks/calibration.
    if (state.dyno.chart) {
      state.dyno.chart.reset();
      state.dyno.maxEffort = 0.05;
      state.dyno.recording = false;
      state.dyno.runNum = 1;
    }
  });

  applyKind($('#f-kind').value);
}

function onConnected() {
  buildGauges();
  if (state.mainChart) state.mainChart.clear();
  openStream();
  refreshHealth();
  if (state.healthTimer) clearInterval(state.healthTimer);
  state.healthTimer = setInterval(refreshHealth, 5000);
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
    if (s.connected) {
      setStatus(true, s);
      onConnected();
    }
  } catch (e) { /* ignore */ }
}

document.addEventListener('DOMContentLoaded', init);
