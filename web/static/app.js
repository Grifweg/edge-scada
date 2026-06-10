'use strict';

// ── CSRF token (from meta tag injected by Flask-WTF) ─────────────────────────

const _csrf = document.querySelector('meta[name="csrf-token"]')?.content ?? '';

// ── Module state ─────────────────────────────────────────────────────────────

let _lastPollMs  = 0;       // epoch ms of last successful poll
let _cfgSeeded   = false;   // config form populated from first status response
let _selectedCh  = 0;       // currently selected Moxa channel
let _alarmHash   = '';      // prevents redundant alarm table redraws

// ── Boot ─────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  _buildChannelSel();
  tickClock();
  setInterval(tickClock, 1000);
  setInterval(tickAge,   1000);
  poll();
  setInterval(poll, 2000);
});

// ── Fetch wrapper — redirects to /login on 401 ───────────────────────────────

async function _apiFetch(url, opts = {}) {
  const r = await fetch(url, opts);
  if (r.status === 401) {
    window.location.href = '/login';
    return null;
  }
  return r;
}

// ── Clock & age ticker ───────────────────────────────────────────────────────

function tickClock() {
  document.getElementById('wall-clock').textContent =
    new Date().toLocaleTimeString('en-GB');
}

function tickAge() {
  if (!_lastPollMs) return;
  const age  = Math.floor((Date.now() - _lastPollMs) / 1000);
  const lost = age > 8;
  document.getElementById('comms-banner').classList.toggle('hidden', !lost);

  const el = document.getElementById('poll-age');
  if (age < 4) {
    el.textContent = 'LIVE';
    el.style.color = 'var(--ok)';
  } else {
    el.textContent = age + 'S AGO';
    el.style.color = lost ? 'var(--fail)' : 'var(--tx-dim)';
  }
}

// ── Polling ──────────────────────────────────────────────────────────────────

async function poll() {
  try {
    const [sr, ar, hr] = await Promise.all([
      _apiFetch('/api/status'),
      _apiFetch('/api/alarms'),
      _apiFetch('/api/health'),
    ]);
    if (sr?.ok) {
      applyStatus(await sr.json());
      _heartbeat();
      _lastPollMs = Date.now();
    }
    if (ar?.ok) applyAlarms(await ar.json());
    if (hr?.ok) applyHealth(await hr.json());
  } catch (_) {
    // tickAge() handles the COMMS LOST banner
  }
}

function _heartbeat() {
  const dot = document.getElementById('hb-dot');
  dot.classList.add('live');
  setTimeout(() => dot.classList.remove('live'), 500);
}

// ── Status renderer ──────────────────────────────────────────────────────────

function applyStatus(d) {
  _renderInetTile(d);
  _renderAlarmTile(d);
  _renderHwTile('plc',  d.plc_ok,  d.config ? d.config.plc_ip : '—',   'FINS TCP');
  _renderHwTile('moxa', d.moxa_ok, d.config ? _moxaSub(d.config) : '—', 'MODBUS TCP');
  _renderOverride(d.override);
  _seedConfigForm(d.config);
}

function _renderInetTile(d) {
  const state = d.inet_state || 'OK';
  const cls   = { OK: 'ok', CONFIRMING: 'warn', FAILED: 'fail' }[state] || '';

  let val, sub;
  if (state === 'CONFIRMING') {
    val = 'CHECKING';
    sub = `CONFIRMING — ${d.fail_count} / 3`;
  } else if (state === 'FAILED') {
    val = 'FAILED';
    sub = `${d.fail_count} CONSECUTIVE FAILURES`;
  } else {
    val = 'OK';
    sub = 'ALL TARGETS REACHABLE';
  }

  _setTile('inet', val, sub, cls, state === 'FAILED', false);
}

function _renderAlarmTile(d) {
  const active   = d.alarm_active;
  const silenced = d.alarm_silenced;
  let sub = active
    ? (silenced ? 'GESILENCEERD' : 'PULSEREND — LAMP & SIRENE')
    : 'OUTPUTS DE-ENERGISED';
  _setTile('alarm',
    active ? 'ACTIVE' : 'CLEAR',
    sub,
    active ? (silenced ? 'warn' : 'fail') : 'ok',
    active && !silenced, active && !silenced);

}

function _renderHwTile(name, ok, ipSub, protocolSub) {
  let val, cls, sub;
  if (ok === null || ok === undefined) {
    val = 'INIT'; cls = '';     sub = protocolSub;
  } else if (ok) {
    val = 'OK';   cls = 'ok';   sub = ipSub;
  } else {
    val = 'ERROR'; cls = 'fail'; sub = ipSub;
  }
  _setTile(name, val, sub, cls, false, false);
}

function _moxaSub(cfg) {
  return `${cfg.moxa_ip}  CH ${cfg.moxa_channel}`;
}

function _setTile(name, val, sub, cls, ledBlink, tilePulse) {
  const tile = document.getElementById(`tile-${name}`);
  const led  = document.getElementById(`led-${name}`);

  let tileCls = 'tile';
  if (cls)       tileCls += ' ' + cls;
  if (tilePulse) tileCls += ' alarm-active';
  tile.className = tileCls;

  let ledCls = 'led';
  if (cls)      ledCls += ' ' + cls;
  if (ledBlink) ledCls += ' blink';
  led.className = ledCls;

  document.getElementById(`val-${name}`).textContent = val;
  document.getElementById(`sub-${name}`).textContent = sub;
}

// ── Health panel renderer ────────────────────────────────────────────────────

function applyHealth(h) {
  if (!h) return;
  _setHealth('flask',    h.flask   !== false);
  _setHealth('engine',   h.engine  !== false);
  _setHealth('watchdog', h.watchdog !== false && h.watchdog !== null);
  _setHealth('plc',      h.plc     === true);
  _setHealth('moxa',     h.moxa    === true);
  _setHealth('internet', h.internet === true);

  const upEl = document.getElementById('uptime-display');
  if (upEl && h.uptime != null) upEl.textContent = _fmtUptime(h.uptime);
}

function _setHealth(name, ok) {
  const item = document.getElementById(`h-${name}`);
  if (!item) return;
  item.className = `health-item ${ok ? 'ok' : 'fail'}`;
  const led = item.querySelector('.led');
  if (led) led.className = `led ${ok ? 'ok' : 'fail'}`;
}

function _fmtUptime(s) {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

// ── Override renderer ────────────────────────────────────────────────────────

function _renderOverride(override) {
  const badge = document.getElementById('mode-badge');
  const desc  = document.getElementById('ovr-desc');

  ['btn-auto', 'btn-force-on', 'btn-force-off'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.remove('active');
  });

  if (override === true) {
    badge.className   = 'mode-badge m-force-on';
    badge.textContent = 'MANUAL — FORCE ON';
    const btn = document.getElementById('btn-force-on');
    if (btn) btn.classList.add('active');
    desc.textContent  = 'Alarm outputs are FORCED ACTIVE regardless of internet state.';
  } else if (override === false) {
    badge.className   = 'mode-badge m-force-off';
    badge.textContent = 'MANUAL — FORCE OFF';
    const btn = document.getElementById('btn-force-off');
    if (btn) btn.classList.add('active');
    desc.textContent  = 'Alarm outputs are FORCED INACTIVE regardless of internet state.';
  } else {
    badge.className   = 'mode-badge m-auto';
    badge.textContent = 'AUTOMATIC';
    const btn = document.getElementById('btn-auto');
    if (btn) btn.classList.add('active');
    desc.textContent  = 'Alarm output follows internet connectivity automatically.';
  }
}

// ── Config form seeding ──────────────────────────────────────────────────────

function _seedConfigForm(cfg) {
  if (_cfgSeeded || !cfg) return;
  document.getElementById('cfg-plc-ip').value           = cfg.plc_ip;
  document.getElementById('cfg-plc-bit').value          = cfg.plc_alarm_bit    || 'W100.00';
  document.getElementById('cfg-moxa-ip').value          = cfg.moxa_ip;
  document.getElementById('cfg-pulse-interval').value   = cfg.moxa_pulse_interval ?? 1.0;
  document.getElementById('cfg-pulse-ack-bit').value    = cfg.moxa_pulse_ack_bit   || 'W100.01';
  _selectChannel(cfg.moxa_channel);
  _cfgSeeded = true;
}

// ── Channel selector ─────────────────────────────────────────────────────────

function _buildChannelSel() {
  const seg = document.getElementById('ch-seg');
  if (!seg) return;
  for (let i = 0; i < 8; i++) {
    const btn = document.createElement('button');
    btn.type        = 'button';
    btn.textContent = String(i);
    btn.className   = 'ch-btn' + (i === 0 ? ' active' : '');
    btn.dataset.ch  = i;
    btn.onclick     = () => _selectChannel(i);
    seg.appendChild(btn);
  }
}

function _selectChannel(n) {
  _selectedCh = n;
  document.querySelectorAll('.ch-btn').forEach(b =>
    b.classList.toggle('active', +b.dataset.ch === n));
}

// ── Alarm renderer ───────────────────────────────────────────────────────────

function applyAlarms(alarms) {
  const h = alarms.map(a => a.ts).join('|');
  if (h === _alarmHash) return;
  _alarmHash = h;

  document.getElementById('alarm-count').textContent =
    alarms.length + (alarms.length === 1 ? ' EVENT' : ' EVENTS');

  const tbody = document.getElementById('alarm-body');
  if (!alarms.length) {
    tbody.innerHTML =
      '<tr><td colspan="4" class="alarm-empty">No events recorded.</td></tr>';
    return;
  }

  tbody.innerHTML = alarms.map(a => {
    const dt  = new Date(a.ts);
    const ts  = dt.toLocaleDateString('en-GB') + '  ' + dt.toLocaleTimeString('en-GB');
    const cls = a.state ? 'alarm-on' : 'alarm-off';
    return (
      `<tr class="${cls}">` +
      `<td>${_esc(ts)}</td>` +
      `<td>${_esc(a.event)}</td>` +
      `<td>${a.state ? 'ON' : 'OFF'}</td>` +
      `<td>${_esc(a.detail || '—')}</td>` +
      `</tr>`
    );
  }).join('');
}

// ── API actions ──────────────────────────────────────────────────────────────

async function setOverride(value) {
  try {
    await _apiFetch('/api/override', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': _csrf },
      body:    JSON.stringify({ value }),
    });
    poll();
  } catch (e) {
    console.error('override:', e);
  }
}

async function saveConfig(e) {
  e.preventDefault();
  const msg = document.getElementById('cfg-msg');
  if (!msg) return;
  _cfgStatus(msg, 'SAVING…', '');

  try {
    const r = await _apiFetch('/api/config', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': _csrf },
      body:    JSON.stringify({
        plc_ip:               document.getElementById('cfg-plc-ip').value.trim(),
        plc_alarm_bit:        document.getElementById('cfg-plc-bit').value.trim(),
        moxa_ip:              document.getElementById('cfg-moxa-ip').value.trim(),
        moxa_channel:         _selectedCh,
        moxa_pulse_interval:  parseFloat(document.getElementById('cfg-pulse-interval').value) || 1.0,
        moxa_pulse_ack_bit:   document.getElementById('cfg-pulse-ack-bit').value.trim(),
      }),
    });
    if (!r) return;   // 401 — already redirected
    const data = await r.json();
    if (data.ok) {
      _cfgStatus(msg, 'APPLIED', 'ok');
      _cfgSeeded = false;   // re-seed on next poll
    } else {
      _cfgStatus(msg, data.error || 'ERROR', 'err');
    }
  } catch (_) {
    _cfgStatus(msg, 'FAILED', 'err');
  }

  setTimeout(() => { if (msg) { msg.textContent = ''; msg.className = 'cfg-msg'; } }, 4000);
}

function _cfgStatus(el, text, cls) {
  el.textContent = text;
  el.className   = 'cfg-msg' + (cls ? ' ' + cls : '');
}

// ── Utilities ────────────────────────────────────────────────────────────────

const _esc = (() => {
  const d = document.createElement('div');
  return s => { d.textContent = String(s); return d.innerHTML; };
})();
