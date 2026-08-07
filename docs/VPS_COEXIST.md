# Triple-bot VPS port map (same host as rubaih + rubaih-greeks)
#
# | Stack            | Public UI/API | Local API | Postgres (host) | Redis (host) |
# |------------------|---------------|-----------|-----------------|--------------|
# | rubaih (futures) | :8080         | :8010     | 127.0.0.1:5433  | 127.0.0.1:6380 |
# | rubaih-greeks    | :8088         | :8018     | 127.0.0.1:5438  | 127.0.0.1:6388 |
# | apex_trader      | :8090         | (in 8090) | 127.0.0.1:5439  | 127.0.0.1:6389 |
#
# Do NOT share Redis/Postgres across stacks. Each compose has its own network.

## Deploy on the shared VPS

```bash
cd ~/apex_trader   # or clone path
cp .env.example .env   # if needed — set secrets, keep paper/shadow first
docker compose up -d --build
curl -s http://127.0.0.1:8090/api/health
```

Public: `http://YOUR_VPS_IP:8090`

## Mobile / browser

Use host `http://YOUR_VPS_IP:8090` (not 8080 — that is Rubaih futures).

## Conflicts avoided

- Default Apex used host `:8080`, `:5432`, `:6379` — those clash with Rubaih nginx and common system services.
- Compose now publishes **8090 / 5439 / 6389** only on the host; container-internal ports stay 8080/5432/6379.
