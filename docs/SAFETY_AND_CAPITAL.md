# Safety changes and capital sizing

## Before updating a running installation

The backup branch `backup/master-before-safety-fixes-2026-09-30` preserves the previous master.
Repository updates do not deploy themselves. Stop autonomous entries, inspect the real broker's
orders and positions, and complete/reconcile outstanding trades before restarting or changing mode.
Keep a database/data-directory backup as well: a Git branch does not back up the trading ledger.

Set a new, long random `API_ACCESS_KEY` in the server environment. Previous versions embedded it
in dashboard HTML, so rotate any key used by a publicly accessible installation. The dashboard
now uses the browser's login prompt: username `apex`, password your `API_ACCESS_KEY`. Serve it
over HTTPS. The key is no longer injected into HTML or Kite login query strings. API clients
can continue sending `X-API-Key` or a Bearer token. A missing key disables authenticated access.

## ₹10,000 does not mean every rupee must be invested

Trade value is shares × price. Planned loss is shares × (entry − stop), plus estimated costs and
slippage. These are different numbers. Several positions totalling ₹5,000–₹6,000 can be normal
for a ₹10,000 account when the risk limits, price/lot rounding and available signals restrict
the allocation. Unused cash is intentional; the engine does not force 100% utilisation.

Defaults after this change:

| Setting | Default | Effect for a ₹10,000 risk-capital basis |
|---|---:|---|
| MAX_POSITION_VALUE_PCT | 25 | At most ₹2,500 per position |
| CASH_RESERVE_PCT | 10 | At most ₹9,000 total position cost, subject to lower available cash |
| MAX_RISK_PER_TRADE_PCT | 0.5 | ₹50 estimated loss budget; costs/slippage reduce shares |
| MIN_NET_REWARD_RISK | 1.5 | Reject reward below 1.5 times stressed stop loss after estimates |
| ESTIMATED_ROUND_TRIP_COST_BPS | 10 | Configurable estimate, not an exact broker charge calculation |
| ESTIMATED_EXIT_SLIPPAGE_BPS | 5 | Configurable adverse-exit allowance |

The same trade must fit remaining daily-loss and portfolio-risk limits. The smallest share
quantity is rejected if it cannot fit; there is no forced one-share override. Margin/collateral
or `MIS_SIZING_LEVERAGE` no longer increases the sizing budget. The old code capped positions at
28% of buying power below ₹15,000 and 15% above it, which could enlarge individual trades when
buying power was inflated. It also double-counted some margin fields.

Set `INITIAL_CAPITAL` to the actual capital allocated to this trader before first initialization.
Changing the environment does not overwrite an existing database ledger. An existing incorrect
capital baseline needs a separately audited ledger correction while flat; do not reset the peak
automatically to hide losses. Margin synchronization updates available cash/buying power only,
because available margin is not account NAV. Deposits/withdrawals require ledger reconciliation.

Stops do not guarantee a maximum realised loss: gaps and outages may exceed the estimate.

## Changed failure behaviour

- Failed/disconnected broker position queries are UNKNOWN, not an empty account. Reconciliation
  preserves the ledger and blocks entries. Missing/extra quantities and unsupported short exposure
  require broker-history review rather than automatic deletion or invented breakeven exits.
- Live submissions are sent once. Timeout/ambiguous responses keep durable pending/unknown intent,
  latch a halt, and require reconciliation before another submission. This implementation favours
  manual recovery over guessing whether a missing response means rejection.
- A target exit cancels and confirms the protective stop, then verifies broker net quantity before
  selling. Partial exits book only confirmed quantities and attempt residual protection.
- Stop acceptance is checked. If protection cannot be confirmed, the actual fill remains visible
  and trading halts. Review/close the exposure at the broker; a halt alone does not flatten it.
- Emergency and EOD closures use confirmed fills. Rejected/unknown orders, remaining broker
  positions and pending intents prevent a successful-flat report. Untracked broker exposures are
  reported for operator intervention; the app does not blindly liquidate unrelated account trades.
- EOD failure is retried, but unknown exit intents prevent unsafe resubmission. A normal target or
  stop exit remains possible while entry gates are halted. Operate only one API worker/replica:
  order sequencing uses one process lock; distributed order coordination is not implemented.
- Peak equity is preserved. Margin changes cannot erase recorded drawdown.

## Performance validation

`POST /api/backtest/validate` now requires real Kite historical candles. The backtester uses the
next bar for entry, examines each subsequent bar, applies adverse gaps/slippage, takes the stop
first if both stop and target appear in one candle, uses position sizing, and retains all trades.
The five-bar time exit remains a research assumption; this is not an exchange replay simulator.
Walk-forward needs profitable out-of-sample trades with a minimum sample. Synthetic demonstrations
cannot enable live execution. Cost assumptions must be calibrated against broker statements.

Shadow mode now stores entries and completed net exits, reloads them on restart, and reports
completed trades and active trading days. Legacy fill-only logs are not accepted as evidence.
Live requires fresh real-data validation and at least the configured completed shadow trades
(30 default), active days (14 default), and positive shadow PnL. `GOLIVE_APPROVED` is operator
consent only; it cannot bypass these checks. Failure to meet them is expected to block live.

Backtest/shadow results remain estimates, not proof of future profitability. Broker outages,
late fills, stop cancellation races and crash recovery need supervised paper/shadow validation
on the deployment infrastructure before live operation.

## Tests

Run `python -m pytest tests -q`. Fixtures use temporary SQLite storage, a fake Redis service and
temporary evidence/shadow logs; production PostgreSQL and broker credentials are not needed.
GitHub Actions runs the suite on pushes to master and pull requests. These tests do not replace
a PostgreSQL/deployment integration test or a supervised broker session.

## Passive trading desk

The control dashboard polls only `GET /api/desk/snapshot`, once per minute by default
(minimum 60 seconds, or a slower configured interval). It reads portfolio memory and
observations published by existing background work. Refresh does not query Kite, Redis or
PostgreSQL, scan markets, run readiness checks, rebuild reports or take the execution lock.
It still uses a small HTTP request on the API process; this is not a separate trading service.
Use the documented single API worker so its observations match the trading process.

The browser pauses polling when hidden, deduplicates overlapping requests, throttles manual
refreshes to at most one every five seconds, bounds reads to eight seconds and backs off on
failure (up to five minutes). Controls are explicit POST actions and retain backend gates.
POST requests are never automatically retried. Opening a page does not start trading.

Freshness reflects existing observations, not a new broker verification. Missing/old P&L and
unknown reconciliation are shown explicitly. A saved Kite session is not labelled connected.
Capital means ledger capital, exposure means tracked entry cost, and stop IDs do not certify
working protection. There is no invented performance curve or estimated margin-use widget.
The top banner and timestamps must be checked before relying on displayed figures.
