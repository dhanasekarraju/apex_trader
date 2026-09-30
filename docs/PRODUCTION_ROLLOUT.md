# Controlled rollout for tn88seval.in

This is an operational checklist, not a certification that the strategy is profitable or safe
for unattended live use. Keep new entries paused until every applicable check passes. Do not
compress the required shadow history to meet a one-hour deadline.

## Separate route

Nautilus owns `/apex/` and Freqtrade owns `/freqtrade/`, `/api/` and `/assets/` in the supplied
Nginx configuration. Keep those blocks intact. Add the contents of
`deploy/nginx-apex-trader.location.conf` **inside the existing HTTPS server block**.
The new Apex URL is `https://tn88seval.in/apex-trader/`, upstream `127.0.0.1:8090`.
Inspect actual port/container mappings before applying: a free port was not verified remotely.

Merge `deploy/apex-trader.env.example` into the existing server environment. Do not replace
its secrets or database connection values. Register the exact new callback URL in the Kite
application: `https://tn88seval.in/apex-trader/api/kite/callback`. Rotate `API_ACCESS_KEY`
if the old public dashboard exposed it. Dashboard login is username `apex` and that key.
Do not run the old bootstrap installer against a shared Nginx site.

## Before replacing a running image

1. Stop new scanning and confirm at Kite that the account has **no positions or pending orders**.
   Check any other bots sharing the account too. A dashboard with zero internal positions is
   insufficient. Do not use a deployment restart as a way to flatten positions.
2. Record the current Git commit and image ID. Make restricted-permission backups of `.env`,
   `data/`, the active Nginx file (including symlink target) and PostgreSQL with `pg_dump`.
   Verify that the dump is readable before continuing. Git branches do not back up trading state.
3. Verify there is only one Apex API worker/container using this database. Kubernetes now uses
   one replica with `Recreate`; do not use an overlapping rolling update. The new PostgreSQL
   advisory lock rejects a second live writer sharing the same database. It does not coordinate
   unrelated applications or accounts using different databases.
4. Keep `AUTONOMOUS_AUTO_START=false` during rollout. Do not change `INITIAL_CAPITAL` to reset
   an existing ledger. Reconcile cash/capital and any old unresolved trade records first.
5. Build the reviewed commit with its revision baked into the image:

   ```sh
   APEX_REVISION=$(git rev-parse HEAD) docker compose build api
   ```

6. Replace only the reviewed Apex API service when flat. Do not recreate all stacks or run
   `docker compose down -v`. Redis volume/config changes need a separately controlled service
   replacement; preserve the old data and confirm the durable operator-pause flag afterward.
7. Back up/edit the existing HTTPS block, run `sudo nginx -t`, and reload Nginx only if it passes.
   Confirm Nautilus and Freqtrade routes still behave as before. Do not overwrite their auth files.

## Evidence required after restart

Run from the repository on the server:

```sh
docker compose exec -T api python scripts/production-preflight.py --expected-revision "$(git rev-parse HEAD)"
```

This performs read-only database, broker and running-API checks. It never starts trading or
changes orders. Failures are blockers, not invitations to disable the checks. In particular,
real backtest evidence and profitable completed shadow history remain mandatory for live.

Also verify, on the actual infrastructure:

- The `/api/health` revision matches the reviewed image; authenticated `/api/ready` returns 200.
  Liveness is not readiness, and readiness is not permission for an order to execute.
- An operator pause survives API and Redis restarts, and automatic startup cannot clear it.
- Missing database/ledger recovery prevents startup; a second process cannot own live execution.
- Broker disconnects, rejected stops, partial fills, EOD closure and restart recovery pass supervised
  paper/shadow exercises with separate database, Redis and data-directory storage.
- Alerts reach the operator, time is correct, disk has room, backups can be restored, and the
  server's outbound IP matches the broker registration.

A live process can no longer run chaos/stress scenarios or auto-refresh chaos evidence.
Generate genuine evidence in an isolated paper deployment and review it. Never copy simulated
success output into validation files or bypass gates to force an opening-day trade.

## Failure recovery

Uncertain orders, partial protective-stop executions, missing protection and unmatched exposure
block new entries and retain ledger evidence. Some cases intentionally require manual broker
order-history reconciliation; fully automatic recovery is not claimed. A completed stop is not
working protection for a remaining position. Stops are checked for symbol, side, product,
exchange and unfilled quantity. No new protective order is blindly repeated after a timeout.

Loss of the ownership connection blocks subsequent Kite writes; existing exchange stops remain
at the broker. Inspect broker state, repair the database connection and restart only after the
owner is released and the account is reconciled. Broker writes are not a database transaction,
so supervision of ambiguous in-flight outcomes remains necessary.

If the new image fails to start, keep entries stopped. Restore the previous application image
and Nginx configuration only after inspecting broker orders. Do not automatically restore an old
trading database over newer fills. The new `operator_controls` table is additive; preserve it.


## Browser authentication changes

Trading POSTs reject untrusted browser origins. Kite login now binds the callback to a secure,
HTTP-only browser nonce cookie, using Kite's documented `redirect_params` mechanism. Start
login from the HTTPS dashboard; opening an old callback link directly will be rejected.
The callback never echoes raw broker exception text into the browser URL.
Reference: https://kite.trade/docs/connect/v3/user/#login-flow
