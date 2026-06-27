# Checkout Cascade (Phase 1 — chaos demo)

Two faulty services + a load generator on Render that produce **realistic, organic incident data**:

```
loadgen (worker) → checkout-service → payment-service → Postgres
```

The story: **checkout looks broken, but the real root cause is a slow DB call inside payment-service.**
Checkout only ever sees "timeout calling payment-service" — the DB root cause is visible only in
payment-service's logs and `/status`. (Phase 2, not in this repo, feeds this into SuperPlane → AI
incident summary → Slack.)

## Deploy on Render (Blueprint — one click)

1. Push this repo to GitHub.
2. Render Dashboard → **New → Blueprint** → pick this repo. Render reads `render.yaml` and creates
   **4 resources**: `payment-service` (web), `checkout-service` (web), `loadgen` (worker),
   `checkout-cascade-db` (Postgres). Click **Apply**.
3. All env vars are auto-wired: the DB URL (`fromDatabase`), the internal service URLs
   (`fromService`), and a generated shared `CHAOS_ADMIN_KEY` (env group `chaos-shared`).
4. After deploy: copy `CHAOS_ADMIN_KEY` from the `chaos-shared` env group, and note the public URLs
   of `payment-service` and `checkout-service`. `loadgen` immediately starts driving ~2 req/s.

> Paid Starter tier (background workers are paid-only; paid web avoids sleeping). Single instance +
> `--workers 1` are required — chaos/metrics state is in-memory per process.

## Endpoints

`payment-service` (POST `/charge`) and `checkout-service` (POST `/checkout`):
- `GET /health` — liveness (chaos-exempt; uses a separate DB pool so chaos can't restart the service)
- `GET /status` — rich evidence JSON: rolling-60s requests/errors/error_rate, latency p50/p95,
  (payment) db_query_ms p50/p95, active chaos, recent events. This is the reliable evidence source.
- `POST /admin/chaos/{flag}/{value}` — header `X-Chaos-Key: <key>`
- `POST /admin/chaos/reset` — header `X-Chaos-Key: <key>`

## Chaos flags (tunable, server-clamped)

**payment-service:** `db_slowdown` (seconds, ≤12 — real `pg_sleep`), `random_error_rate` (0–1),
`memory_leak` (bool, capped ~200 MB), `payment_failure` (bool → clean 502)
**checkout-service:** `own_latency` (seconds, ≤10), `random_error_rate` (0–1)

## Demo (reset between modes)

```bash
KEY=<from chaos-shared env group>
PAY=https://payment-service-XXXX.onrender.com
CHK=https://checkout-service-XXXX.onrender.com

# baseline
curl $PAY/status; curl $CHK/status

# HERO: 6s DB slowdown (> checkout's 3s timeout) → checkout times out, root cause hidden in payment
curl -X POST $PAY/admin/chaos/db_slowdown/6 -H "X-Chaos-Key: $KEY"
curl $CHK/status     # error_rate up; recent[] shows cause=payment_timeout
curl $PAY/status     # db_query_ms.p95 ~6000  ← the real cause; /health still ok (not restarted)
curl -X POST $PAY/admin/chaos/reset -H "X-Chaos-Key: $KEY"

# clean 500s
curl -X POST $PAY/admin/chaos/random_error_rate/1.0 -H "X-Chaos-Key: $KEY"
curl -X POST $PAY/admin/chaos/reset -H "X-Chaos-Key: $KEY"

# memory leak (watch Render memory graph trend up; memory_leak_mb rises)
curl -X POST $PAY/admin/chaos/memory_leak/true -H "X-Chaos-Key: $KEY"
curl -X POST $PAY/admin/chaos/reset -H "X-Chaos-Key: $KEY"

# independence: checkout's own errors don't touch payment
curl -X POST $CHK/admin/chaos/random_error_rate/0.5 -H "X-Chaos-Key: $KEY"
curl -X POST $CHK/admin/chaos/reset -H "X-Chaos-Key: $KEY"
```

> Render's HTTP latency/error graphs are measured at the edge router and may not reflect
> private-network (internal) traffic — so during an incident lean on `/status` (richer anyway) and
> the **memory** graph. CPU/memory graphs always move.

## Run locally

```bash
# 1. Postgres
docker run -d --name cc-pg -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=checkout_cascade -p 5432:5432 postgres:16

# 2. payment-service (terminal A)
cd payment-service && pip install -r requirements.txt
DATABASE_URL=postgresql://postgres:pw@localhost:5432/checkout_cascade CHAOS_ADMIN_KEY=dev \
  uvicorn main:app --port 8001

# 3. checkout-service (terminal B)
cd checkout-service && pip install -r requirements.txt
PAYMENT_SERVICE_URL=localhost:8001 CHAOS_ADMIN_KEY=dev uvicorn main:app --port 8002

# 4. drive it
curl -X POST localhost:8002/checkout -H 'Content-Type: application/json' -d '{"user_id":"demo","amount":49.99}'
curl -X POST localhost:8001/admin/chaos/db_slowdown/6 -H 'X-Chaos-Key: dev'
curl localhost:8002/checkout -X POST -d '{"user_id":"demo","amount":1}' -H 'Content-Type: application/json'  # times out
curl localhost:8001/status
```

Design + spec: `docs/superpowers/specs/2026-06-27-checkout-cascade-design.md`.
Implementation plan (with tests): `docs/superpowers/plans/2026-06-27-checkout-cascade.md`.
