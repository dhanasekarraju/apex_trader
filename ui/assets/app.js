function resolveApexBase() {
  if (window.APEX_BASE) return String(window.APEX_BASE).replace(/\/$/, '');
  const p = window.location.pathname || '';
  if (p === '/apex' || p.startsWith('/apex/')) return '/apex';
  return '';
}

const API = resolveApexBase();

const POLL_MS = Math.max(Number(window.APEX_UI_POLL_MS) || 60000, 15000);

function authHeaders(extra = {}) {
  const headers = { 'Content-Type': 'application/json', ...extra };

  return headers;
}

async function api(path, opts = {}) {
  const r = await fetch(API + path, {
    headers: authHeaders(opts.headers || {}),
    ...opts,
  });
  const text = await r.text();
  let payload;
  try {
    payload = text ? JSON.parse(text) : {};
  } catch {
    if (!r.ok) throw new Error(text || r.statusText);
    return text;
  }
  if (payload && typeof payload.success === 'boolean') {
    if (!payload.success) throw new Error(payload.error || 'Request failed');
    return payload.data ?? {};
  }
  if (!r.ok) throw new Error(text || r.statusText);
  return payload;
}

function kiteConnectHref(status) {
  if (status?.login_url) return status.login_url;
  const login = `${API}/api/kite/login`;

  return login;
}

async function loadKiteStatus() {
  const panel = document.getElementById('kiteStatusPanel');
  const pill = document.getElementById('kitePill');
  const connectBtn = document.getElementById('kiteConnectBtn');
  const disconnectBtn = document.getElementById('kiteDisconnectBtn');
  const hint = document.getElementById('kiteRedirectHint');
  if (!panel || !connectBtn || !disconnectBtn) return;

  try {
    const s = await api('/api/kite/status');
    pill.textContent = s.connected ? 'CONNECTED' : 'OFFLINE';
    pill.className = 'pill ' + (s.connected ? 'live' : (s.configured ? 'warn' : 'danger'));
    const who = s.user_name ? ` · ${s.user_name}` : '';
    const when = s.login_time
      ? ` · ${new Date(s.login_time).toLocaleString('en-IN')}`
      : '';
    panel.textContent = `${s.message}${who}${when}`;
    if (hint) {
      if (s.redirect_url && s.configured && !s.connected) {
        hint.style.display = 'block';
        hint.textContent = `Redirect URL: ${s.redirect_url}`;
      } else {
        hint.style.display = 'none';
      }
    }
    connectBtn.style.display = s.connected ? 'none' : 'inline-block';
    connectBtn.href = kiteConnectHref(s);
    disconnectBtn.style.display = s.connected ? 'inline-block' : 'none';
  } catch {
    panel.textContent = 'Could not load Kite status — try Connect or check APP_BASE_PATH=/apex';
    pill.textContent = 'Kite —';
    pill.className = 'pill danger';
    connectBtn.style.display = 'inline-block';
    connectBtn.href = kiteConnectHref(null);
    disconnectBtn.style.display = 'none';
  }
}

function handleKiteQueryParams() {
  const params = new URLSearchParams(window.location.search);
  const kite = params.get('kite');
  if (!kite) return;
  if (kite === 'connected') {
    alert('Kite connected. You can start autonomous.');
  } else if (kite === 'error') {
    alert('Kite login failed: ' + decodeURIComponent(params.get('reason') || 'unknown'));
  }
  window.history.replaceState({}, '', API + '/');
}

async function disconnectKite() {
  if (!confirm('Disconnect Kite on this server?')) return;
  await api('/api/kite/disconnect', { method: 'POST' });
  await refreshAll();
}

function renderAutonomous(status, liveBlockers) {
  if (!status) return;
  const running = !!status.running;
  const pill = document.getElementById('autonomousPill');
  if (pill) {
    pill.textContent = running ? 'AUTO — RUNNING' : 'AUTO — OFF';
    pill.className = 'pill ' + (running ? 'live' : '');
  }

  const sessionEl = document.getElementById('autoSession');
  if (sessionEl) sessionEl.textContent = status.session || '—';

  const wl = document.getElementById('autoWatchlist');
  if (wl) {
    wl.textContent = status.watchlist_mode === 'dynamic'
      ? `${status.universe_scan_size || 15} scan · pool ${status.universe_pool_size || 50}`
      : `${status.watchlist_count ?? 0} symbols`;
  }

  const last = document.getElementById('autoLastCycle');
  if (last) {
    last.textContent = status.last_cycle_at
      ? (status.last_cycle_at.slice(11, 19) || status.last_cycle || '—')
      : (status.last_cycle || '—');
  }

  const stats = status.stats || {};
  const cycle = document.getElementById('autoCycleStats');
  if (cycle) {
    cycle.textContent = stats.scanned != null
      ? `${stats.scanned} / ${stats.buy ?? 0} buy`
      : '—';
  }

  const blockers = [...new Set([...(status.blockers || []), ...(liveBlockers || [])])];
  const blockersEl = document.getElementById('autoBlockers');
  const actionsEl = document.getElementById('autoBlockerActions');
  if (blockersEl) {
    if (blockers.length && !running) {
      blockersEl.style.display = 'block';
      blockersEl.textContent = 'Blockers: ' + blockers.join('; ');
    } else {
      blockersEl.style.display = 'none';
      blockersEl.textContent = '';
    }
  }
  if (actionsEl) {
    const needsCrce = blockers.some(b => /CRCE|repair-chain/i.test(b));
    const needsChaos = blockers.some(b => /chaos|resilience|INSTITUTIONAL|scenario/i.test(b));
    actionsEl.style.display = blockers.length && !running ? 'flex' : 'none';
    const crceBtn = document.getElementById('repairCrceBtn');
    const chaosBtn = document.getElementById('runChaosBtn');
    if (crceBtn) crceBtn.style.display = needsCrce ? 'inline-block' : 'none';
    if (chaosBtn) chaosBtn.style.display = needsChaos ? 'inline-block' : 'none';
  }

  const startBtn = document.getElementById('autoStartBtn');
  const stopBtn = document.getElementById('autoStopBtn');
  if (startBtn) {
    startBtn.style.display = running ? 'none' : 'inline-block';
    startBtn.title = blockers.length ? blockers.join('; ') : 'Start autonomous scan';
  }
  if (stopBtn) stopBtn.style.display = running ? 'inline-block' : 'none';
}

async function loadAutonomousPanel() {
  try {
    const [status, checklist] = await Promise.all([
      api('/api/autonomous/status'),
      api('/api/live/checklist').catch(() => ({})),
    ]);
    const liveBlockers = checklist.crce_and_chaos || checklist.hard_blockers || [];
    renderAutonomous(status, liveBlockers);
  } catch (e) {
    console.error('autonomous', e);
  }
}

async function repairCrce() {
  const btn = document.getElementById('repairCrceBtn');
  const prev = btn?.textContent;
  try {
    if (btn) { btn.textContent = 'Repairing…'; btn.disabled = true; }
    const r = await api('/api/live/repair-crce', { method: 'POST' });
    alert(r.repair?.message || (r.crce_ok ? 'CRCE OK' : 'Repair done'));
    await refreshAll();
  } catch (e) {
    alert('CRCE repair failed: ' + (e.message || e));
  } finally {
    if (btn) { btn.textContent = prev || 'Repair CRCE'; btn.disabled = false; }
  }
}

async function runFullChaos() {
  if (!confirm('Run full chaos suite (~5–15 min)? Stop autonomous first.')) return;
  const btn = document.getElementById('runChaosBtn');
  const prev = btn?.textContent;
  try {
    if (btn) { btn.textContent = 'Starting…'; btn.disabled = true; }
    await api('/api/autonomous/stop', { method: 'POST' }).catch(() => {});
    await api('/api/chaos/run?quick=false&background=true', { method: 'POST' });
    alert('Chaos started in background. Refresh in ~10 min, then start autonomous.');
  } catch (e) {
    alert('Chaos failed: ' + (e.message || e));
  } finally {
    if (btn) { btn.textContent = prev || 'Run chaos suite'; btn.disabled = false; }
  }
}

async function startAutonomous() {
  let blockers = [];
  try {
    const status = await api('/api/autonomous/status');
    blockers = status.blockers || [];
  } catch (e) {
    alert('Could not load status: ' + (e.message || e));
    return;
  }
  if (blockers.length) {
    alert('Start blocked:\n\n' + blockers.map((b, i) => `${i + 1}. ${b}`).join('\n'));
    return;
  }
  if (!confirm('Start autonomous?\n\nScans watchlist and trades through risk + execution.')) return;
  try {
    const r = await api('/api/autonomous/start', { method: 'POST' });
    alert(r.message || 'Autonomous started.');
    await loadAutonomousPanel();
  } catch (e) {
    alert(e.message || 'Start failed');
    await loadAutonomousPanel();
  }
}

async function stopAutonomous() {
  await api('/api/autonomous/stop', { method: 'POST' });
  await loadAutonomousPanel();
}

async function loadRiskPanel() {
  try {
    const d = await api('/api/dashboard');
    const p = d.portfolio || {};
    document.getElementById('riskSummary').textContent = `Mode: ${d.mode} · Capital ₹${p.equity} · Available cash ₹${p.cash} · Daily P&L ₹${p.daily_pnl} · Drawdown ${p.drawdown_pct}% · ${p.trading_halted ? 'HALTED' : 'Active'} · Reconciliation: ${d.reconciliation?.status || 'UNKNOWN'} ${d.reconciliation?.reason || ''}`;
    const positions = d.positions || [];
    document.getElementById('positionsSummary').textContent = positions.length ? positions.map(p => `${p.symbol}: ${p.qty} shares · entry ₹${p.entry} · stop ₹${p.stop_loss} · target ₹${p.take_profit} · ${p.stop_order_id ? 'stop ID ' + p.stop_order_id : 'protection unconfirmed'}`).join('\n') : 'No internally tracked positions. Check broker reconciliation status before live trading.';
    const s = d.sizing_policy || {};
    document.getElementById('sizingSummary').textContent = `Cash-based position cap ${s.max_position_value_pct}% · cash reserve ${s.cash_reserve_pct}% · per-trade risk ${s.max_risk_per_trade_pct}% · minimum net reward/risk ${s.min_net_reward_risk}. Latest decision: ${d.recent_decisions?.[0]?.sizing?.detail || 'none'}`;
  } catch (e) {
    document.getElementById('riskSummary').textContent = 'State unavailable — do not assume the account is flat. ' + e.message;
  }
}
async function emergencyFlatten() {
  if (!confirm('Halt entries and attempt to close tracked positions? Unconfirmed exits will remain flagged.')) return;
  const result = await api('/api/admin/kill-switch/on', {method: 'POST'});
  alert(result.ok ? 'Confirmed tracked exits completed; trading remains halted.' : 'Exit incomplete. Check broker positions and unresolved orders.');
  await refreshAll();
}
async function refreshAll() {
  await Promise.all([loadKiteStatus(), loadAutonomousPanel(), loadRiskPanel()]);
}

handleKiteQueryParams();
refreshAll();
setInterval(refreshAll, POLL_MS);
