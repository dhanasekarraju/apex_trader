const API = window.APEX_BASE || '';
const API_KEY = window.APEX_API_KEY || '';

function authHeaders(extra = {}) {
  const headers = { 'Content-Type': 'application/json', ...extra };
  if (API_KEY) headers['X-API-Key'] = API_KEY;
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

function fmt(n, d = 2) {
  if (n == null || isNaN(n)) return '—';
  return Number(n).toLocaleString('en-IN', { maximumFractionDigits: d });
}

function fmtRs(n) {
  return '₹' + fmt(n);
}

function renderEquityCurve(points) {
  const canvas = document.getElementById('equityChart');
  if (!canvas || !points?.length) return;
  const ctx = canvas.getContext('2d');
  const w = canvas.width = canvas.parentElement.clientWidth - 32;
  const h = canvas.height = 120;
  ctx.clearRect(0, 0, w, h);
  const vals = points.map(p => p.equity);
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const range = max - min || 1;
  ctx.strokeStyle = '#22c55e';
  ctx.lineWidth = 2;
  ctx.beginPath();
  vals.forEach((v, i) => {
    const x = (i / (vals.length - 1 || 1)) * (w - 20) + 10;
    const y = h - 10 - ((v - min) / range) * (h - 20);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function renderReadiness(report) {
  const el = document.getElementById('readinessPanel');
  const pill = document.getElementById('readinessPill');
  if (!report) return;
  const passed = report.overall_passed;
  pill.textContent = passed ? 'Go-live READY' : 'Go-live BLOCKED';
  pill.className = 'pill ' + (passed ? 'live' : 'danger');
  const cats = (report.categories || []).map(c => `
    <div class="gate ${c.passed ? 'ok' : 'fail'}">
      <span>${c.passed ? '✓' : '✗'}</span>
      <span><strong>${c.name}</strong> — ${c.score?.toFixed?.(0) ?? c.score}% · ${c.details}</span>
    </div>`).join('');
  el.innerHTML = `
    <div style="margin-bottom:10px;font-size:13px;color:${passed ? 'var(--green)' : 'var(--red)'}">
      ${report.recommendation}
    </div>
    ${report.blockers?.length ? `<div class="metric-sub" style="margin-bottom:8px">Blockers: ${report.blockers.join('; ')}</div>` : ''}
    <div class="gates">${cats}</div>`;
}

function renderStrategyRanking(rows) {
  const el = document.getElementById('strategyPanel');
  if (!rows?.length) {
    el.innerHTML = '<div class="empty">No strategies registered</div>';
    return;
  }
  el.innerHTML = `<table><thead><tr><th>Strategy</th><th>Win%</th><th>PnL</th><th>Status</th></tr></thead><tbody>
    ${rows.map(r => `<tr>
      <td>${r.name}</td>
      <td>${r.win_rate}%</td>
      <td>${fmtRs(r.total_pnl)}</td>
      <td><span class="badge ${r.enabled ? 'buy' : 'reject'}">${r.enabled ? 'ON' : 'OFF'}</span></td>
    </tr>`).join('')}
  </tbody></table>`;
}

function renderShadow(shadow) {
  const el = document.getElementById('shadowPanel');
  if (!shadow) return;
  el.innerHTML = `
    <div class="grid grid-2" style="gap:10px">
      <div><div class="metric-sub">Simulated fills</div><div class="metric">${shadow.simulated_fills ?? 0}</div></div>
      <div><div class="metric-sub">Missed opportunities</div><div class="metric amber">${shadow.missed_opportunities ?? 0}</div></div>
      <div><div class="metric-sub">Avg slippage</div><div style="font-family:var(--mono)">${shadow.avg_slippage_bps ?? 0} bps</div></div>
      <div><div class="metric-sub">Shadow win rate</div><div style="font-family:var(--mono)">${shadow.win_rate ?? 0}%</div></div>
    </div>`;
}

function renderJournal(j) {
  const el = document.getElementById('journalPanel');
  if (!j) return;
  el.innerHTML = `
    <div class="grid grid-2" style="gap:10px">
      <div><div class="metric-sub">Decisions</div><div class="metric">${j.total_decisions ?? 0}</div></div>
      <div><div class="metric-sub">Closed trades</div><div class="metric">${j.closed_trades ?? 0}</div></div>
      <div><div class="metric-sub">Win rate</div><div class="metric green">${j.win_rate ?? 0}%</div></div>
      <div><div class="metric-sub">Total PnL</div><div class="metric">${fmtRs(j.total_pnl ?? 0)}</div></div>
    </div>`;
}

function kiteConnectHref(status) {
  if (status?.login_url) return status.login_url;
  if (API_KEY) return `${API}/api/kite/login?api_key=${encodeURIComponent(API_KEY)}`;
  return '#';
}

async function loadKiteStatus() {
  const panel = document.getElementById('kiteStatusPanel');
  const pill = document.getElementById('kitePill');
  const connectBtn = document.getElementById('kiteConnectBtn');
  const disconnectBtn = document.getElementById('kiteDisconnectBtn');
  const hint = document.getElementById('kiteRedirectHint');
  if (!panel) return;
  if (!connectBtn || !disconnectBtn) return;
  try {
    const s = await api('/api/kite/status');
    pill.textContent = s.connected ? 'Kite CONNECTED' : 'Kite OFFLINE';
    pill.className = 'pill ' + (s.connected ? 'live' : (s.configured ? 'warn' : 'danger'));
    const who = s.user_name ? ` · ${s.user_name}` : '';
    const when = s.login_time ? ` · logged in ${new Date(s.login_time).toLocaleString('en-IN')}` : '';
    panel.innerHTML = `<span style="color:${s.connected ? 'var(--green)' : 'var(--amber)'}">${s.message}${who}${when}</span>`;
    if (s.redirect_url && s.configured) {
      hint.style.display = 'block';
      hint.textContent = `Register this redirect URL on developers.kite.trade: ${s.redirect_url}`;
    }
    if (connectBtn) {
      connectBtn.style.display = s.connected ? 'none' : 'inline-block';
      connectBtn.removeAttribute('aria-disabled');
      connectBtn.href = kiteConnectHref(s);
    }
    if (disconnectBtn) {
      disconnectBtn.style.display = s.connected ? 'inline-block' : 'none';
    }
  } catch (e) {
    panel.textContent = 'Unable to load Kite session status';
    pill.textContent = 'Kite —';
    pill.className = 'pill danger';
    if (connectBtn) {
      connectBtn.style.display = 'inline-block';
      connectBtn.removeAttribute('aria-disabled');
      connectBtn.href = '#';
    }
  }
}

function handleKiteQueryParams() {
  const params = new URLSearchParams(window.location.search);
  const kite = params.get('kite');
  if (!kite) return;
  if (kite === 'connected') {
    alert('Zerodha Kite connected successfully. Session saved for today.');
  } else if (kite === 'error') {
    const reason = params.get('reason') || 'unknown';
    alert('Kite login failed: ' + decodeURIComponent(reason));
  }
  window.history.replaceState({}, '', (window.APEX_BASE || '') + '/');
}

async function disconnectKite() {
  if (!confirm('Disconnect Zerodha Kite session on this server?')) return;
  await api('/api/kite/disconnect', { method: 'POST' });
  loadKiteStatus();
  loadDashboard();
}

async function loadDashboard() {
  try {
    const [d, readiness] = await Promise.all([
      api('/api/dashboard'),
      api('/api/readiness'),
    ]);
    const p = d.portfolio || {};
    document.getElementById('mEquity').textContent = fmtRs(p.equity);
    const pnlEl = document.getElementById('mPnl');
    pnlEl.textContent = (p.daily_pnl >= 0 ? '+' : '') + fmtRs(p.daily_pnl);
    pnlEl.className = 'metric ' + (p.daily_pnl >= 0 ? 'green' : 'red');
    document.getElementById('mDrawdown').textContent = fmt(p.drawdown_pct) + '%';
    document.getElementById('mPositions').textContent = p.open_positions ?? 0;
    document.getElementById('mHeatSub').textContent = 'Portfolio heat ' + fmt(p.portfolio_heat_pct) + '%';
    document.getElementById('heatPill').textContent = 'Heat ' + fmt(p.portfolio_heat_pct) + '%';
    document.getElementById('modeSelect').value = d.mode || 'paper';

    const halted = !!(p.trading_halted || p.emergency_halt || p.black_swan_mode);
    const resumeBtn = document.getElementById('resumeBtn');
    if (resumeBtn) resumeBtn.style.display = halted ? 'inline-block' : 'none';
    const modePill = document.getElementById('modePill');
    if (halted) {
      modePill.textContent = 'HALTED';
      modePill.className = 'pill danger';
    } else {
      modePill.textContent = (d.mode || 'paper').toUpperCase();
      modePill.className = 'pill live';
    }

    const pr = document.getElementById('principles');
    pr.innerHTML = (d.principles || []).map(t =>
      `<span class="principle">${t}</span>`).join('');

    renderEquityCurve(d.equity_curve || []);
    renderReadiness(readiness);
    renderStrategyRanking(d.strategy_ranking || []);
    renderShadow(d.shadow_report);
    renderJournal(d.journal_weekly);
    renderDecisions(d.recent_decisions || []);
  } catch (e) {
    console.error(e);
  }
}

function renderDecisions(rows) {
  const body = document.getElementById('decisionsBody');
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="7" class="empty">No decisions yet</td></tr>';
    return;
  }
  body.innerHTML = rows.map(d => {
    const cls = d.action === 'BUY' ? 'buy' : (d.action === 'REJECTED' ? 'reject' : 'wait');
    return `<tr>
      <td>${d.symbol}</td>
      <td><span class="badge ${cls}">${d.action}</span></td>
      <td>${d.regime || '—'}</td>
      <td>${d.strategy || '—'}</td>
      <td>${d.ai_confidence ?? '—'}</td>
      <td>${d.risk_verdict || '—'}</td>
      <td>${d.qty ?? '—'}</td>
    </tr>`;
  }).join('');
}

async function analyzeSymbol() {
  const sym = document.getElementById('symbolInput').value.trim().toUpperCase();
  if (!sym) return;
  const el = document.getElementById('analysisResult');
  el.innerHTML = '<div class="empty">Analyzing through full pipeline…</div>';

  try {
    const [decision, regime] = await Promise.all([
      api('/api/analyze', { method: 'POST', body: JSON.stringify({ symbol: sym }) }),
      api('/api/regime/' + sym),
    ]);

    document.getElementById('regimePill').textContent = 'Regime ' + (regime.regime || '—');
    document.getElementById('regimePanel').innerHTML = `
      <div class="metric" style="font-size:1.2rem;margin-bottom:8px">${regime.regime}</div>
      <div class="metric-sub">${regime.explanation}</div>
      <div style="margin-top:12px;font-size:12px;color:var(--muted)">
        Confidence ${regime.confidence}% · Vol ${regime.volatility_pct}% ·
        Trade ${regime.trade_allowed ? 'allowed' : 'blocked'}
      </div>
      <div style="margin-top:8px;font-size:11px;color:var(--cyan)">
        Strategies: ${(regime.recommended_strategies || []).join(', ') || 'none'}
      </div>`;

    const cls = decision.action === 'BUY' ? 'buy' : 'reject';
    const checks = (decision.risk_checks || []).map(c =>
      `<div class="gate ${c.passed ? 'ok' : 'fail'}">
        <span>${c.passed ? '✓' : '✗'}</span>
        <span><strong>${c.name}</strong> — ${c.detail}</span>
      </div>`).join('');

    el.innerHTML = `
      <div style="margin-bottom:12px">
        <span class="badge ${cls}">${decision.action}</span>
        <span style="margin-left:8px;font-family:var(--mono);font-size:12px">${decision.risk_reason || decision.reason || ''}</span>
      </div>
      <div class="grid grid-2" style="gap:12px;margin-bottom:12px">
        <div><div class="metric-sub">Strategy</div><div style="font-family:var(--mono)">${decision.strategy || '—'}</div></div>
        <div><div class="metric-sub">AI confidence</div><div style="font-family:var(--mono);color:var(--cyan)">${decision.ai_confidence ?? '—'}</div></div>
        <div><div class="metric-sub">Entry / SL / Target</div><div style="font-family:var(--mono);font-size:11px">${decision.entry} / ${decision.stop_loss} / ${decision.take_profit}</div></div>
        <div><div class="metric-sub">Approved qty</div><div style="font-family:var(--mono)">${decision.qty ?? 0}</div></div>
      </div>
      <h2 style="font-size:10px;margin-bottom:8px">Risk checks</h2>
      <div class="gates">${checks || '<div class="empty">No checks</div>'}</div>`;

    loadDashboard();
  } catch (e) {
    el.innerHTML = '<div class="empty">Analysis failed — is API running?</div>';
  }
}

async function runBacktest() {
  const sym = document.getElementById('btSymbol').value.trim().toUpperCase();
  const strategy = document.getElementById('btStrategy').value || null;
  const el = document.getElementById('backtestResult');
  el.innerHTML = '<div class="empty">Running backtest…</div>';
  try {
    const r = await api('/api/backtest', {
      method: 'POST',
      body: JSON.stringify({ symbol: sym, strategy }),
    });
    const pass = r.passed_validation;
    el.innerHTML = `
      <div class="grid grid-2" style="gap:10px">
        <div><div class="metric-sub">Trades</div><div class="metric" style="font-size:1.2rem">${r.total_trades}</div></div>
        <div><div class="metric-sub">Win rate</div><div class="metric green" style="font-size:1.2rem">${r.win_rate}%</div></div>
        <div><div class="metric-sub">Net return</div><div class="metric ${r.net_return_pct>=0?'green':'red'}" style="font-size:1.2rem">${r.net_return_pct}%</div></div>
        <div><div class="metric-sub">Max DD</div><div class="metric amber" style="font-size:1.2rem">${r.max_drawdown}%</div></div>
        <div><div class="metric-sub">Sharpe / Sortino / Calmar</div><div style="font-family:var(--mono);font-size:11px">${r.sharpe} / ${r.sortino} / ${r.calmar}</div></div>
        <div><div class="metric-sub">Profit factor</div><div style="font-family:var(--mono)">${r.profit_factor}</div></div>
      </div>
      <div style="margin-top:10px;font-size:12px;color:${pass?'var(--green)':'var(--red)'}">
        Validation: ${pass ? 'PASSED' : 'FAILED'} · WF ${r.walk_forward_passed?'✓':'✗'} · MC ${r.monte_carlo_passed?'✓':'✗'}
      </div>
      ${r.rejection_reasons?.length ? `<div class="metric-sub" style="margin-top:6px">${r.rejection_reasons.join('; ')}</div>` : ''}`;
  } catch (e) {
    el.innerHTML = '<div class="empty">Backtest failed</div>';
  }
}

async function switchMode(mode) {
  try {
    await api('/api/mode', { method: 'POST', body: JSON.stringify({ mode }) });
    loadDashboard();
  } catch (e) {
    alert('Mode switch blocked: ' + e.message);
    loadDashboard();
  }
}

function statusClass(status) {
  const s = (status || 'SAFE').toUpperCase();
  if (s === 'HALTED') return 'status-halted';
  if (s === 'DANGER') return 'status-danger';
  if (s === 'WARNING') return 'status-warning';
  return 'status-safe';
}

function renderPositions(positions) {
  const body = document.getElementById('positionsBody');
  if (!body) return;
  if (!positions?.length) {
    body.innerHTML = '<tr><td colspan="8" class="empty">No open positions</td></tr>';
    return;
  }
  body.innerHTML = positions.map(p => {
    const pnlCls = p.unrealized_pnl >= 0 ? 'green' : 'red';
    return `<tr>
      <td>${p.symbol}</td>
      <td>${fmt(p.qty, 0)}</td>
      <td>${fmtRs(p.avg_price)}</td>
      <td>${fmtRs(p.ltp)}</td>
      <td class="${pnlCls}">${fmtRs(p.unrealized_pnl)}</td>
      <td>${p.strategy || '—'}</td>
      <td>${p.source || '—'}</td>
    </tr>`;
  }).join('');
}

function renderTradeStream(events) {
  const el = document.getElementById('tradeStream');
  if (!el) return;
  if (!events?.length) {
    el.innerHTML = '<div class="empty">No trade events yet</div>';
    return;
  }
  el.innerHTML = events.map(e => `
    <div class="event">
      <span>${(e.timestamp || '').slice(11, 19) || '—'}</span>
      <span>${e.action || '—'}</span>
      <span>${e.symbol || '—'} · ${e.strategy || '—'} · ${e.result || ''} ${e.message || ''}</span>
    </div>`).join('');
}

async function loadControlPanel() {
  try {
    const [pnl, risk, trades] = await Promise.all([
      api('/api/risk/pnl/live'),
      api('/api/risk/status'),
      api('/api/risk/trades/recent?limit=20'),
    ]);

    const portPnl = document.getElementById('livePortfolioPnl');
    const dailyPnl = document.getElementById('liveDailyPnl');
    if (portPnl) {
      portPnl.textContent = (pnl.portfolio_pnl >= 0 ? '+' : '') + fmtRs(pnl.portfolio_pnl);
      portPnl.className = 'metric ' + (pnl.portfolio_pnl >= 0 ? 'green' : 'red');
    }
    if (dailyPnl) {
      dailyPnl.textContent = (pnl.daily_pnl >= 0 ? '+' : '') + fmtRs(pnl.daily_pnl);
      dailyPnl.className = 'metric ' + (pnl.daily_pnl >= 0 ? 'green' : 'red');
    }
    document.getElementById('liveExposure').textContent = fmt(pnl.open_exposure) + '%';
    document.getElementById('liveMargin').textContent = fmt(pnl.margin_used) + '%';

    const status = risk.status || 'SAFE';
    const statusEl = document.getElementById('riskStatusLabel');
    const sysPill = document.getElementById('systemStatusPill');
    const cls = statusClass(status);
    if (statusEl) {
      statusEl.textContent = status;
      statusEl.className = 'metric ' + cls;
    }
    if (sysPill) {
      sysPill.textContent = 'System ' + status;
      sysPill.className = 'pill ' + cls;
    }

    const halted = !!risk.kill_switch;
    document.getElementById('killSwitchLabel').textContent = halted ? 'ON' : 'OFF';
    document.getElementById('killSwitchLabel').className = 'metric ' + (halted ? 'red' : 'green');
    const killBtn = document.getElementById('killSwitchBtn');
    if (killBtn) {
      killBtn.classList.toggle('active', halted);
      killBtn.textContent = halted ? 'HALTED' : 'KILL SWITCH';
      killBtn.disabled = halted;
    }
    const resumeBtn = document.getElementById('resumeBtn');
    if (resumeBtn) resumeBtn.style.display = halted ? 'inline-block' : 'none';

    const pnlPill = document.getElementById('livePnlPill');
    if (pnlPill) {
      pnlPill.textContent = 'PnL ' + fmtRs(pnl.portfolio_pnl);
      pnlPill.className = 'pill ' + (pnl.portfolio_pnl >= 0 ? 'live' : 'danger');
    }

    renderPositions(pnl.positions || []);
    renderTradeStream(trades.events || []);

    const analyzeBtn = document.querySelector('#symbolInput + .btn, .input-row .btn');
    document.querySelectorAll('.input-row input, .input-row select, .input-row .btn').forEach(el => {
      if (halted) el.setAttribute('disabled', 'disabled');
      else el.removeAttribute('disabled');
    });
    loadAutonomousPanel();
  } catch (e) {
    console.error('control panel', e);
  }
}

async function killSwitchOn() {
  if (!confirm('ACTIVATE KILL SWITCH?\n\nThis will cancel all orders, flatten positions, and block all trading.')) return;
  await api('/api/admin/kill-switch/on', { method: 'POST' });
  alert('Kill switch activated.');
  loadDashboard();
  loadControlPanel();
}

async function emergencyShutdown() {
  return killSwitchOn();
}

async function emergencyFlatten() {
  if (!confirm('FLATTEN ALL and enter black swan mode?')) return;
  const r = await api('/api/admin/flatten-all', { method: 'POST' });
  alert('Flatten complete. Positions closed: ' + (r.flattened ?? 0));
  loadDashboard();
  loadControlPanel();
}

async function resumeTrading() {
  if (!confirm('Clear kill switch and resume trading? Only do this after reviewing what triggered the stop.')) return;
  const r = await api('/api/admin/kill-switch/off', { method: 'POST' });
  alert(r.message || 'Trading resumed.');
  loadDashboard();
  loadControlPanel();
  loadAutonomousPanel();
}

function renderAutonomous(status) {
  if (!status) return;
  const running = !!status.running;
  const pill = document.getElementById('autonomousPill');
  if (pill) {
    pill.textContent = running ? 'AUTO — RUNNING' : 'AUTO — OFF';
    pill.className = 'pill ' + (running ? 'live' : '');
  }
  document.getElementById('autoSession').textContent = status.session || '—';
  document.getElementById('autoWatchlist').textContent =
    (status.watchlist_count ?? 0) + ' symbols' + (status.watchlist_preview?.length ? ' · ' + status.watchlist_preview.slice(0, 4).join(', ') : '');
  document.getElementById('autoLastCycle').textContent =
    status.last_cycle_at ? (status.last_cycle_at.slice(11, 19) || status.last_cycle) : (status.last_cycle || '—');
  const stats = status.stats || {};
  document.getElementById('autoCycleStats').textContent =
    stats.scanned != null ? `${stats.scanned} / ${stats.buy ?? 0} buy` : '—';

  const blockersEl = document.getElementById('autoBlockers');
  const blockers = status.blockers || [];
  if (blockersEl) {
    if (blockers.length && !running) {
      blockersEl.style.display = 'block';
      blockersEl.textContent = 'Blockers: ' + blockers.join('; ');
    } else {
      blockersEl.style.display = 'none';
      blockersEl.textContent = '';
    }
  }

  const recentEl = document.getElementById('autoRecent');
  const recent = status.recent || [];
  if (recentEl) {
    if (!recent.length) {
      recentEl.innerHTML = `<div class="empty">${running ? 'Scanning watchlist…' : 'Autonomous idle — start to scan watchlist'}</div>`;
    } else {
      recentEl.innerHTML = recent.slice().reverse().map(r =>
        `<div class="event"><span>${r.symbol}</span> · <span>${r.action}</span> · ${(r.reason || r.strategy || '').slice(0, 48)}</div>`
      ).join('');
    }
  }

  const startBtn = document.getElementById('autoStartBtn');
  const stopBtn = document.getElementById('autoStopBtn');
  const halted = document.getElementById('killSwitchLabel')?.textContent === 'ON';
  if (startBtn) {
    startBtn.style.display = running ? 'none' : 'inline-block';
    startBtn.disabled = !!halted;
    startBtn.title = blockers.length
      ? 'Blocked: ' + blockers.join('; ')
      : 'Start autonomous watchlist scan';
    startBtn.classList.toggle('blocked', blockers.length > 0 && !halted);
  }
  if (stopBtn) stopBtn.style.display = running ? 'inline-block' : 'none';
}

async function loadAutonomousPanel() {
  try {
    const status = await api('/api/autonomous/status');
    renderAutonomous(status);
  } catch (e) {
    console.error('autonomous panel', e);
  }
}

async function startAutonomous() {
  let blockers = [];
  try {
    const status = await api('/api/autonomous/status');
    blockers = status.blockers || [];
  } catch (e) {
    alert('Could not load autonomous status: ' + (e.message || e));
    return;
  }
  if (blockers.length) {
    alert(
      'Autonomous start is blocked in LIVE mode:\n\n'
      + blockers.map((b, i) => `${i + 1}. ${b}`).join('\n')
      + '\n\nFix: run the full chaos suite on the server, then retry.'
    );
    return;
  }
  if (!confirm('Start autonomous engine?\n\nIt will scan the watchlist and route every signal through the full risk + execution pipeline.')) return;
  try {
    const r = await api('/api/autonomous/start', { method: 'POST' });
    alert(r.message || 'Autonomous engine started.');
    loadAutonomousPanel();
  } catch (e) {
    alert(e.message || 'Failed to start autonomous engine');
    loadAutonomousPanel();
  }
}

async function stopAutonomous() {
  await api('/api/autonomous/stop', { method: 'POST' });
  loadAutonomousPanel();
}

async function copySupportBundle() {
  const btn = document.getElementById('supportBundleBtn');
  const prev = btn?.textContent;
  try {
    if (btn) { btn.textContent = 'Loading…'; btn.disabled = true; }
    const r = await fetch(API + '/api/support/incident-bundle?format=text', {
      headers: authHeaders(),
    });
    if (!r.ok) throw new Error(await r.text() || r.statusText);
    const text = await r.text();
    await navigator.clipboard.writeText(text);
    if (btn) btn.textContent = 'Copied!';
    setTimeout(() => { if (btn) { btn.textContent = prev || 'Copy logs'; btn.disabled = false; } }, 2500);
  } catch (e) {
    if (btn) { btn.textContent = prev || 'Copy logs'; btn.disabled = false; }
    alert('Could not copy logs: ' + (e.message || e) + '\n\nEnsure API key is set (APEX_API_KEY in page config).');
  }
}

loadKiteStatus();
handleKiteQueryParams();
loadDashboard();
loadControlPanel();
loadAutonomousPanel();
setInterval(loadControlPanel, 3000);
setInterval(loadDashboard, 30000);
setInterval(loadKiteStatus, 60000);
