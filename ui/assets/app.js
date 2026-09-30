/* Passive desk: one bounded snapshot request, no broker calls during refresh. */
const API = window.APEX_BASE || (location.pathname === '/apex' || location.pathname.startsWith('/apex/') ? '/apex' : '');
const POLL_MS = Math.max(60000, Number(window.APEX_UI_POLL_MS) || 60000);
const $ = id => document.getElementById(id);
const money = n => Number.isFinite(n) ? '₹' + n.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2}) : '—';
const pct = n => Number.isFinite(n) ? n.toFixed(1) + '%' : '—';
const time = value => value ? new Date(value).toLocaleTimeString('en-IN', {timeZone:'Asia/Kolkata', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false}) : '—';
const set = (id, value) => { $(id).textContent = value; };
const node = (tag, text, cls) => { const el = document.createElement(tag); if (text != null) el.textContent = text; if (cls) el.className = cls; return el; };
let snapshot = null, receivedAt = 0, inFlight = null, timer = null, failures = 0, lastAttempt = 0, actionBusy = false;

async function api(path, method = 'GET') {
  const response = await fetch(API + path, {method, credentials:'same-origin', cache:'no-store',
    ...(method === 'GET' ? {signal:AbortSignal.timeout(8000)} : {})});
  const body = await response.json();
  if (!response.ok || body.success === false) throw new Error(body.error || body.detail || 'Request failed (' + response.status + ')');
  return body.success === true ? body.data : body;
}
function badge(id, text, tone = '') { set(id, text); $(id).className = 'badge ' + tone; }
function observationFresh(observation, maxAge = 120) {
  return observation?.age_seconds != null && observation.age_seconds + (Date.now() - receivedAt) / 1000 < maxAge;
}
function renderFreshness() {
  if (!snapshot) return;
  const d = snapshot, age = (Date.now() - receivedAt) / 1000;
  const feedFresh = observationFresh(d.pnl) && !d.pnl.data.stale;
  const issues = [];
  if (failures || age > 120) issues.push('Snapshot unavailable or old; displayed values may be stale.');
  if (!feedFresh) issues.push('P&L observation is stale or unavailable.');
  if (d.portfolio.trading_halted) issues.push('Trading is halted. Existing exposure still needs supervision.');
  if (d.reconciliation.status !== 'OK') issues.push('Reconciliation ' + d.reconciliation.status + ': ' + (d.reconciliation.reason || 'not confirmed'));
  if (!Object.values(d.loops).every(Boolean)) issues.push('One or more trading loops are not running.');
  const notice = $('stateNotice');
  notice.className = 'notice ' + (d.portfolio.trading_halted || d.reconciliation.status === 'DEGRADED' ? 'bad' : issues.length ? '' : 'good');
  notice.textContent = issues.length ? issues.join(' ') : 'Latest observations available. ' + (d.mode === 'paper' ? 'Paper mode uses simulated execution.' : d.mode === 'shadow' ? 'Shadow mode does not place live orders.' : 'Live mode selected; order eligibility is checked by the backend.');
  const runFresh = observationFresh(d.running, Math.max(120, d.scan_interval_seconds * 2 + 30));
  set('engineState', runFresh ? (d.running.data.running ? 'Scanning enabled' : 'Scanning stopped') : 'Unknown / stale');
  $('engineDot').className = 'status-dot ' + (runFresh && d.running.data.running && d.loops.autonomous ? 'good' : '');
  $('startBtn').disabled = actionBusy || !runFresh || !!d.running.data.running || !!d.portfolio.trading_halted || d.reconciliation.status !== 'OK' || !feedFresh || failures > 0 || age > 120;
  set('dataSource', (d.mode === 'paper' ? 'Paper observations' : 'Engine observations') + ' · P&L ' + time(d.pnl.observed_at) + ' IST' + (feedFresh ? '' : ' · STALE'));
  if (!feedFresh) { set('dayPnl', '—'); set('pnlDetail', 'Stale P&L withheld · last observation ' + time(d.pnl.observed_at)); }
}
function meter(label, value, limit, unit = '%') {
  const wrapper = node('div', null, 'meter');
  const line = node('div', null, 'meter-line');
  line.append(node('span', label), node('b', (Number.isFinite(value) ? value.toFixed(unit ? 1 : 0) + unit : '—') + ' / ' + limit + unit));
  const track = node('div', null, 'meter-track');
  const ratio = Number.isFinite(value) && limit > 0 ? value / limit * 100 : 0;
  const fill = node('div', null, 'meter-fill ' + (ratio >= 90 ? 'bad' : ratio >= 70 ? 'warn' : ''));
  fill.style.width = Math.max(0, Math.min(100, ratio)) + '%'; track.append(fill); wrapper.append(line, track); return wrapper;
}
function render(d) {
  const p = d.portfolio, policy = d.sizing_policy, positions = d.positions;
  const feedFresh = observationFresh(d.pnl) && !d.pnl.data.stale;
  const pnl = d.pnl.data;
  badge('modeBadge', String(d.mode).toUpperCase() + ' MODE', d.mode === 'live' ? 'warn' : '');
  set('capital', money(p.equity)); set('cash', money(p.cash));
  set('dayPnl', feedFresh ? money(pnl.daily_pnl) : '—');
  $('dayPnl').className = 'metric-value ' + (feedFresh && pnl.daily_pnl > 0 ? 'positive' : feedFresh && pnl.daily_pnl < 0 ? 'negative' : '');
  set('pnlDetail', feedFresh ? 'Realised ' + money(pnl.realized_pnl) + ' · unrealised ' + money(pnl.unrealized_pnl) : 'Awaiting fresh P&L');
  const exposure = positions.reduce((sum, pos) => sum + pos.qty * pos.entry, 0);
  const deployed = p.equity > 0 ? exposure / p.equity * 100 : null;
  set('exposure', money(exposure)); set('exposureDetail', positions.length + ' positions · ' + pct(deployed) + ' of ledger capital');
  set('allocationPct', pct(deployed) + ' deployed'); set('allocatedValue', money(exposure)); set('unallocatedValue', money(Math.max(0, p.equity - exposure)));
  $('allocationFill').style.width = Math.min(100, Math.max(0, deployed || 0)) + '%';
  set('allocationNote', 'Position-cost ceiling ' + (100 - policy.cash_reserve_pct) + '% · actual sizing can be lower. Unallocated capital is not available margin.');
  set('positionCount', positions.length);
  const rows = [];
  for (const pos of positions) {
    const row = node('tr');
    const name = node('td', pos.symbol); name.append(node('small', pos.strategy)); row.append(name);
    row.append(node('td', pos.qty, 'numeric'), node('td', money(pos.entry), 'numeric'));
    const levels = node('td', money(pos.stop_loss), 'numeric'); levels.append(node('small', money(pos.take_profit))); row.append(levels);
    const quote = (pnl.positions || []).find(q => q.symbol === pos.symbol && q.qty === pos.qty);
    const gain = feedFresh && quote ? quote.unrealized_pnl : null;
    row.append(node('td', money(gain), 'numeric ' + (gain > 0 ? 'positive' : gain < 0 ? 'negative' : '')));
    const stop = node('td'); const label = node('span', pos.stop_order_id ? 'ID recorded' : 'Unconfirmed', 'badge ' + (pos.stop_order_id ? '' : 'warn'));
    label.title = pos.stop_order_id || 'No protective stop ID recorded'; stop.append(label); row.append(stop); rows.push(row);
  }
  if (!rows.length) { const tr = node('tr'), td = node('td', 'No internally tracked positions. Broker reconciliation determines whether the account is flat.', 'empty'); td.colSpan = 6; tr.append(td); rows.push(tr); }
  $('positionRows').replaceChildren(...rows);
  const lossPct = p.equity > 0 && feedFresh ? Math.max(0, -pnl.daily_pnl) / p.equity * 100 : null;
  $('riskMeters').replaceChildren(meter('Daily loss', lossPct, policy.max_daily_loss_pct), meter('Ledger drawdown', Math.max(0, p.drawdown_pct), policy.max_monthly_drawdown_pct), meter('Planned portfolio risk', p.portfolio_heat_pct, policy.max_portfolio_heat_pct), meter('Open positions', positions.length, policy.max_open_positions, ''));
  badge('riskBadge', p.trading_halted ? 'HALTED' : 'LIMITS', p.trading_halted ? 'bad' : '');
  set('tradeRisk', policy.max_risk_per_trade_pct + '%'); set('netPayoff', policy.min_net_reward_risk + ' : 1'); set('positionCap', policy.max_position_value_pct + '%'); set('cashReserve', policy.cash_reserve_pct + '%');
  set('reconcileState', d.reconciliation.status); $('reconcileState').className = d.reconciliation.status === 'OK' ? '' : 'negative';
  set('kiteState', d.kite.session_saved ? 'Saved · not verified here' : d.kite.configured ? 'Login needed' : 'Not configured'); set('sessionTime', d.session);
  const auto = d.autonomous.data;
  set('lastCycle', time(d.autonomous.observed_at)); set('scanStats', auto.stats ? (auto.stats.scanned ?? '—') + ' / ' + (auto.stats.buy ?? '—') : '—');
  const decisions = d.recent_decisions.map(item => { const el = node('div', null, 'decision'), body = node('div', null, 'decision-body'); body.append(node('div', item.symbol + ' · ' + (item.strategy || 'Strategy unavailable'), 'decision-title'), node('p', item.risk_reason || 'No decision detail recorded.')); el.append(node('span', '↗', 'decision-mark'), body, node('span', item.action || 'OBSERVED', 'badge ' + (item.action === 'REJECTED' ? 'warn' : ''))); return el; });
  $('decisionList').replaceChildren(...(decisions.length ? decisions : [node('p', 'No signal decisions in this process yet. The desk does not trigger scans.', 'empty')]));
  renderFreshness();
}
function schedule() { clearTimeout(timer); if (!document.hidden) timer = setTimeout(refresh, Math.max(POLL_MS, Math.min(300000, POLL_MS * 2 ** Math.min(failures, 3)))); }
function refresh() {
  if (inFlight) return inFlight;
  if (Date.now() - lastAttempt < 5000) { schedule(); return Promise.resolve(); }
  clearTimeout(timer); lastAttempt = Date.now(); $('refreshBtn').disabled = true; set('refreshStatus', 'Reading snapshot…');
  inFlight = (async () => {
    try { const d = await api('/api/desk/snapshot'); snapshot = d; receivedAt = Date.now(); failures = 0; render(d); set('refreshStatus', 'Snapshot ' + time(d.generated_at) + ' IST · every 60s'); }
    catch (error) { failures++; set('refreshStatus', 'Update unavailable · retrying less often'); if (snapshot) renderFreshness(); else { set('stateNotice', 'Snapshot unavailable. ' + error.message); $('startBtn').disabled = true; } }
    finally { inFlight = null; $('refreshBtn').disabled = false; schedule(); }
  })();
  return inFlight;
}
function confirmAction(title, description) {
  const dialog = $('confirmDialog'); set('confirmTitle', title); set('confirmText', description); dialog.returnValue = '';
  return new Promise(resolve => { dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), {once:true}); dialog.showModal(); });
}
async function action(path, title, description) {
  if (actionBusy) return;
  actionBusy = true;
  try {
    if (!await confirmAction(title, description)) return;
    for (const id of ['startBtn','stopBtn','haltBtn']) $(id).disabled = true;
    const notice = $('actionNotice'); notice.hidden = false; notice.className = 'notice action-notice'; notice.textContent = 'Waiting for backend confirmation. Do not repeat this action.';
    const result = await api(path, 'POST');
    notice.textContent = result.ok === false ? (result.message || (result.blockers || []).join('; ') || 'Action incomplete. Review broker orders and positions.') : path.includes('kill-switch') ? 'Halt processed. ' + (result.ok === true ? 'Tracked closure confirmed by backend. Review broker account for any untracked exposure.' : 'Review closure results and broker exposure; a halt does not prove the account is flat.') : result.message || 'Request completed. Snapshot will reflect the observed engine state.';
    notice.className = 'notice action-notice ' + (result.ok === false ? 'bad' : '');
    if (inFlight) await inFlight;
    lastAttempt = 0; await refresh();
  } catch (error) { $('actionNotice').hidden = false; $('actionNotice').className = 'notice action-notice bad'; set('actionNotice', 'Action outcome is unconfirmed. Check engine / broker state before retrying. ' + error.message); }
  finally { actionBusy = false; $('stopBtn').disabled = false; $('haltBtn').disabled = false; renderFreshness(); }
}
$('refreshBtn').addEventListener('click', refresh);
$('startBtn').addEventListener('click', () => action('/api/autonomous/start', 'Start autonomous scanning?', 'The backend will check trading eligibility. Eligible live-mode signals may place real orders.'));
$('stopBtn').addEventListener('click', () => action('/api/autonomous/stop', 'Stop autonomous scanning?', 'Existing positions remain open. Protective stops and lifecycle processing remain the backend’s responsibility.'));
$('haltBtn').addEventListener('click', () => action('/api/admin/kill-switch/on', 'Halt and close tracked positions?', 'This is a real trading action in live mode. Unconfirmed or partial exits require broker review.'));
$('kiteConnect').href = API + '/api/kite/login';
set('deskDate', new Date().toLocaleDateString('en-IN', {timeZone:'Asia/Kolkata', day:'2-digit', month:'short', year:'numeric'}));
const params = new URLSearchParams(location.search);
if (params.has('kite')) { $('actionNotice').hidden = false; set('actionNotice', params.get('kite') === 'connected' ? 'Kite login completed. Engine checks still determine trading eligibility.' : 'Kite login failed. ' + (params.get('reason') || 'Please retry login.')); history.replaceState({}, '', API + '/'); }
document.addEventListener('visibilitychange', () => { clearTimeout(timer); if (!document.hidden) { renderFreshness(); refresh(); } });
// Local freshness only; this timer never makes a request.
setInterval(() => { if (!document.hidden) renderFreshness(); }, 10000);
if (!document.hidden) refresh();
