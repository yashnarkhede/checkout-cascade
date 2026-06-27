# Checkout Cascade — Phase 1 Design Spec

**Date:** 2026-06-27
**Status:** Approved design (pre-implementation) — revised after a 4-agent verification pass (Render facts, Python/async facts, completeness review, demo red-team).
**Scope:** Phase 1 only — build and deploy two faulty services + a load generator that produce believable, organic incident data on Render. Phase 2 (SuperPlane incident-AI pipeline) is out of scope and noted only where it shapes phase-1 outputs.

> **All decisions resolved.** The two HTTP hops use Render **internal** URLs (Option A, §11.5): clean one-click Blueprint auto-wiring; the incident is shown via the rich `/status` JSON (the phase-2 evidence source) + Render CPU/memory graphs. Render HTTP latency/error graphs may not reflect private-network traffic, which is an accepted trade-off.

---

## 1. Summary

A chaos-engineering demo on Render that tells a realistic incident story: **checkout looks broken, but the real root cause is a degraded database call inside payment-service.**

- `checkout-service` (web) calls `payment-service` (web) over HTTP.
- `payment-service` is the only service with database access; it reads/writes Postgres.
- A `loadgen` background worker drives continuous, jittered, **open-loop** traffic so Render's native metrics and the services' own `/status` show a real baseline and a real incident trend.
- Both web services expose `/admin/chaos/*` endpoints to inject failure modes live, with **tunable (and server-clamped) magnitude**, without redeploying.
- Both services emit a **rich `/status` JSON** (the reliable, primary phase-2 evidence source) and **structured, grep-able logs** (corroborating real telemetry). A `request_id` threads checkout→payment for ground-truth correlation.

Phase 2 (not built here) will pull both services' `/status` (primary) and optionally Render logs/metrics, assemble an evidence pack, and trigger an AI-generated incident summary to Slack.

---

## 2. Goals and non-goals

### Goals
- Four independently deployable Render components (2 web + 1 worker + 1 Postgres), provisioned by a single Blueprint (`render.yaml`).
- `checkout-service` → calls → `payment-service` → reads/writes → Postgres.
- Chaos toggles on both web services, controllable via HTTP without redeploying, with **tunable magnitude (server-clamped to safe bounds)**.
- A load generator producing steady, jittered, open-loop baseline traffic so request volume stays visible **even during an incident**.
- Clean, readable, grep-able logs on both services that diverge meaningfully during an incident.
- Rich `/status` JSON on both services as the primary, reliable evidence interface for phase 2.
- The incident is **visible**: at minimum via `/status` and Render CPU/memory graphs; via Render HTTP latency/error graphs subject to §11.5.
- Deployable today on paid Starter tier, demoable live (dial chaos in front of judges).

### Non-goals (do not build in this phase)
- SuperPlane / Slack / AI reasoning layer (phase 2).
- Redis / centralized chaos state (chaos state is in-memory per process, by design — see §11.4).
- Authentication/user accounts (internal demo tool); only the `X-Chaos-Key` admin guard.
- Frontend UI (curl/`/status` JSON is enough).
- Persisting metrics beyond an in-memory rolling window.
- Horizontal scaling — single instance, single worker per service is a hard requirement (§11.4).

---

## 3. Architecture

```
┌──────────────────────────── Render ────────────────────────────┐
│                                                                 │
│   loadgen (worker) ──POST /checkout (open-loop)──▶ checkout-svc │
│        ~2 req/s + jitter, capped in-flight            │ (web)   │
│                                                       │         │
│                         POST /charge (3s TO, X-Request-Id)      │
│                                                       ▼         │
│                                               payment-service   │
│                                                   (web)         │
│                                                       │         │
│                       asyncpg (charge pool + tiny health pool)  │
│                                                       ▼         │
│                                          checkout-cascade-db    │
│                                       (Postgres basic-256mb)    │
└─────────────────────────────────────────────────────────────────┘
```

- `checkout-service` has **no DB access**. It only knows `payment-service`'s HTTP API.
- `payment-service` is the **only** holder of `DATABASE_URL`.
- DB connection is always Render **internal**. The two HTTP hops are internal or external per §11.5.
- Both web services log to stdout/stderr (captured by Render) and expose `/admin/chaos/*`, `/status`, `/health`.
- `loadgen` keeps both web services warm and generates the traffic the graphs/`/status` need.

### Data flow (happy path)
`loadgen` → `POST /checkout {user_id, amount}` (new `request_id`) → checkout logs + (optional `own_latency`) → `POST payment/charge` with `X-Request-Id`, 3 s timeout → payment logs `charge_received` → INSERT transaction → verifying SELECT → respond success → checkout logs `checkout_success` → returns 200. Both sides record a metrics entry.

### Data flow (hero incident: `db_slowdown`)
Operator sets `db_slowdown=6` on payment → every `/charge` runs `SELECT pg_sleep(6)` (one held connection from the **charge pool**) → payment `/status` p95 + `db_query_ms` spike, logs show `db_query_done duration_ms≈6005` → checkout's 3 s call times out → checkout logs `payment_call_result status=timeout` (it does **not** know about the DB) → checkout `/status` error_rate climbs. `/health` keeps passing (it uses a **separate** tiny pool, §8.2/§11.1), so Render does **not** restart the service. Root cause is visible only in payment-service evidence; the `request_id` links the two sides.

---

## 4. Tech stack & key decisions

| Choice | Decision | Rationale |
|---|---|---|
| Language / framework | Python 3.11+ / FastAPI (lifespan handler) + uvicorn `--workers 1` | fast; async; single worker required for in-memory state |
| DB driver | raw `asyncpg`, **two pools**: charge pool + tiny health pool | health must never be starved by `db_slowdown` (§11.1) |
| Runtime | Render **native** Python runtime | asyncpg needs no OS packages |
| Deploy | single Render **Blueprint** (`render.yaml`) | reproducible; auto-wires DB URL + shared secret (+ internal service URLs, §11.5) |
| Tier | paid **Starter** (web 512 MB + worker), **basic-256mb** Postgres | workers are paid-only; paid web avoids sleep |
| `db_slowdown` impl | SQL-level `pg_sleep(value)`, **server-clamped** to `DB_SLOWDOWN_MAX` | authentic slow-query time; clamp keeps it from exhausting the pool (§11.2) |
| Chaos magnitude | tunable via the value path segment, validated + clamped | deterministic for the judged run; dial for drama within safe bounds |
| Chaos state | in-memory per process, single instance/worker | independence is a goal; §11.4 |
| Evidence source | **rich `/status` JSON (primary) + structured logs (corroborating)** + `request_id` | `/status` pull is reliable; Render log/metric APIs are fragile |
| Concurrency hold time | a held DB connection lasts the **full `db_slowdown`**, not checkout's 3 s | uvicorn does not cancel the handler on client disconnect (verified) |

---

## 5. Repository structure

Monorepo, git repo at `/Users/yash/Documents/checkout-cascade`. Services are **self-contained** (no cross-folder imports) so each Render service's root-directory build is trivial. Small `metrics`/`logging`/`chaos` helpers are duplicated per service intentionally.

```
checkout-cascade/
├── render.yaml                     # Blueprint: 2 web + 1 worker + 1 db, all env wiring
├── README.md                       # endpoints, chaos flags, demo curl script
├── .gitignore
├── docs/superpowers/specs/2026-06-27-checkout-cascade-design.md   # this file
├── loadgen/
│   ├── run.py                      # open-loop scheduler: POST /checkout ~2 rps + jitter, capped in-flight
│   └── requirements.txt
├── checkout-service/
│   ├── main.py                     # FastAPI app, endpoints, shared httpx.AsyncClient
│   ├── chaos.py                    # chaos state + flag parsing/validation
│   ├── metrics.py                  # rolling-window deque + percentiles (guarded)
│   ├── logging_setup.py            # structured one-line logger
│   └── requirements.txt
└── payment-service/
    ├── main.py
    ├── chaos.py
    ├── metrics.py
    ├── logging_setup.py
    ├── db.py                       # charge pool + health pool, startup migration w/ retry, queries
    └── requirements.txt
```

---

## 6. Render deployment — Blueprint

A single `render.yaml` provisions everything. `CHAOS_ADMIN_KEY` is a generated secret shared across the **two web services** via an environment group; service URLs via `fromService`; the DB URL via `fromDatabase`. **All Render field names below were verified against the current Blueprint spec (§14) — no field changes were required.**

```yaml
databases:
  - name: checkout-cascade-db
    plan: basic-256mb            # VERIFIED smallest paid Postgres; 256 MB ⇒ max_connections = 100
    databaseName: checkout_cascade
    user: checkout_cascade

envVarGroups:
  - name: chaos-shared
    envVars:
      - key: CHAOS_ADMIN_KEY
        generateValue: true      # VERIFIED: generated ONCE; identical for every service linking the group

services:
  - type: web
    name: payment-service
    runtime: python              # VERIFIED current field (legacy `env:` discouraged)
    plan: starter                # VERIFIED 512 MB / 0.5 CPU
    numInstances: 1              # REQUIRED: in-memory state (§11.4); do not autoscale
    rootDir: payment-service
    buildCommand: pip install -r requirements.txt
    startCommand: uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1   # single worker REQUIRED
    healthCheckPath: /health     # success = 2xx/3xx within 5s; ~60s of failures ⇒ Render RESTARTS (wipes state)
    envVars:
      - fromGroup: chaos-shared
      - key: DATABASE_URL
        fromDatabase:
          name: checkout-cascade-db
          property: connectionString   # VERIFIED INTERNAL connection string
      - { key: DB_POOL_MAX,            value: "30" }   # charge pool max_size; << 100 max_connections
      - { key: HEALTH_POOL_MAX,        value: "2"  }   # dedicated health pool (never starved)
      - { key: DB_SLOWDOWN_MAX_SECONDS,value: "12" }   # server clamp; keeps rps×value < charge pool (§11.2)
      - { key: DB_STATEMENT_TIMEOUT_MS,value: "20000"} # backstop so a stuck query can't hold forever
      - { key: MEMORY_LEAK_CAP_MB,     value: "200" }  # hard-capped in code too (§11.3)

  - type: web
    name: checkout-service
    runtime: python
    plan: starter
    numInstances: 1
    rootDir: checkout-service
    buildCommand: pip install -r requirements.txt
    startCommand: uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1
    healthCheckPath: /health
    envVars:
      - fromGroup: chaos-shared
      - key: PAYMENT_SERVICE_URL
        fromService:
          name: payment-service
          type: web
          property: hostport     # VERIFIED internal host:port (no scheme); code prepends http://  (§11.5)
      - { key: PAYMENT_TIMEOUT_SECONDS, value: "3" }
      - { key: OWN_LATENCY_MAX_SECONDS, value: "10" }

  - type: worker
    name: loadgen
    runtime: python
    plan: starter                # VERIFIED workers are PAID-ONLY; starter is cheapest. (No chaos group: loadgen has no admin API.)
    rootDir: loadgen
    buildCommand: pip install -r requirements.txt
    startCommand: python run.py
    envVars:
      - key: CHECKOUT_SERVICE_URL
        fromService:
          name: checkout-service
          type: web
          property: hostport     # internal host:port; code prepends http://  (§11.5)
      - { key: LOADGEN_RPS,             value: "2"  }
      - { key: LOADGEN_TIMEOUT_SECONDS, value: "20" }   # must exceed OWN_LATENCY_MAX + PAYMENT_TIMEOUT
      - { key: LOADGEN_MAX_INFLIGHT,    value: "50" }   # cap concurrent in-flight checkouts
```

**Manual fallback** (if not using the Blueprint): create Postgres → payment-service (`DATABASE_URL` from internal connection string, generate `CHAOS_ADMIN_KEY`) → checkout-service (`PAYMENT_SERVICE_URL` = payment internal host:port, same `CHAOS_ADMIN_KEY`) → loadgen worker (`CHECKOUT_SERVICE_URL`). Pin single instance + `--workers 1` everywhere. Blueprint strongly preferred.

---

## 7. Shared conventions

### 7.1 Structured logging
One line per event, leading ISO-8601 UTC millisecond timestamp (trailing `Z`), level, service, event, then `key=value` fields including `request_id` where applicable. Grep-able. (Example below is chronological.)

```
2026-06-27T12:34:56.789Z INFO  payment-service event=charge_received request_id=ab12 user_id=demo-user amount=49.99
2026-06-27T12:34:56.794Z INFO  payment-service event=db_query_start request_id=ab12
2026-06-27T12:35:02.799Z INFO  payment-service event=db_query_done request_id=ab12 duration_ms=6005
2026-06-27T12:35:02.800Z INFO  payment-service event=charge_success request_id=ab12
2026-06-27T12:35:05.000Z WARN  payment-service event=chaos_set flag=db_slowdown value=6.0
2026-06-27T12:35:08.100Z WARN  checkout-service event=payment_call_result request_id=cd34 status=timeout elapsed_ms=3001
```
Chaos flips log at WARN (`event=chaos_set`). Failures log at ERROR with a `cause=` field naming the responsible chaos flag (or `db_error`/`conn_error`).

### 7.2 Rolling-window metrics
Each service keeps a bounded `collections.deque(maxlen=N)` (N≈600) of request records `{ts, request_id, latency_ms, ok, status_code, cause, db_ms?}`. `/status` computes over records within `window_sec` (default 60) by taking a **snapshot copy** of the deque in a single synchronous section (no `await` interleaving), then pruning-by-age and sorting the copy — so concurrent appends can't corrupt the read. Requires `--workers 1` (state is per-process). **Guards (mandatory):**
- empty window or `requests==0` → `error_rate=0.0`, `p50=p95=null`.
- percentile index uses nearest-rank clamped to `[0, n-1]`: `sorted[min(n-1, int(round(q*(n-1))))]`.
Not persisted (phase 2 polls live).

### 7.3 Chaos admin auth
`/admin/chaos/*` require header `X-Chaos-Key: <CHAOS_ADMIN_KEY>`; missing/wrong → `401`. `/charge`, `/checkout`, `/health`, `/status` are unauthenticated.

### 7.4 Chaos value parsing & validation
`POST /admin/chaos/{flag}/{value}`:
- **Per-service flag set:** each service accepts only its own flags (§8.5 / §9.3); any other flag → `400`.
- **Parse:** magnitude flags (`db_slowdown`, `random_error_rate`, `own_latency`) parse as float; bool flags (`memory_leak`, `payment_failure`) accept a case-insensitive token set `{true,false,1,0,yes,no,on,off}`. Unparseable value → `400` with a clear message (never an unhandled 500).
- **Range:** `random_error_rate` clamped to `[0.0, 1.0]`; `db_slowdown` clamped to `[0, DB_SLOWDOWN_MAX_SECONDS]`; `own_latency` clamped to `[0, OWN_LATENCY_MAX_SECONDS]`; negatives rejected → `400`. The response echoes the effective (possibly clamped) value.
- `POST /admin/chaos/reset` zeroes/false-es all flags and clears the leak list (see §11.3 caveat). Both are logged `event=chaos_set`.

---

## 8. payment-service spec

### 8.1 Database schema (startup migration, with retry)
On startup, **awaited inside the FastAPI lifespan handler** (so the app serves no traffic until it succeeds), with bounded retry-with-backoff (the DB may still be provisioning on first Blueprint deploy):
```sql
CREATE TABLE IF NOT EXISTS transactions (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    amount NUMERIC(10,2) NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```
If migration ultimately fails after retries, fail fast (process exits, Render retries the deploy) rather than serving a half-initialized app.

### 8.2 asyncpg pools (two)
- **Charge pool:** `min_size=2`, `max_size=DB_POOL_MAX` (default 30); `max_connections` for basic-256mb is 100, so this has wide headroom. `acquire()` uses a finite timeout so a saturated pool fails fast (503 `cause=pool_timeout`) instead of blocking indefinitely. A session `statement_timeout = DB_STATEMENT_TIMEOUT_MS` is set as a backstop.
- **Health pool:** a separate `min_size=1, max_size=HEALTH_POOL_MAX` (default 2) pool used **only** by `/health`, so a fully-checked-out charge pool (from `pg_sleep`) can never starve the health check (§11.1). Uses 2 of the 100 DB connections.
- A single `/charge` acquires **one** charge-pool connection and runs `pg_sleep` + INSERT + verifying SELECT on it, so hold time ≈ `db_slowdown` (not multiple acquisitions). Hold time equals the full `db_slowdown` because the server handler is not cancelled when checkout's 3 s client times out (verified).

### 8.3 Endpoints
| Method | Path | Behavior |
|---|---|---|
| POST | `/charge` | Body `{user_id, amount}`, header `X-Request-Id`. Apply chaos (8.4), DB work, return `{status, transaction_id}`. Records metrics on **every** path. |
| GET | `/health` | Fast `SELECT 1` via the **health pool** with a short timeout. 200 `{"status":"ok"}` if DB reachable, else 503. **Exempt from all chaos and from charge-pool contention.** |
| GET | `/status` | Rich evidence JSON (8.6). Unauthenticated. |
| POST | `/admin/chaos/{flag}/{value}` | Set a flag (auth). |
| POST | `/admin/chaos/reset` | Reset all flags + clear leak list (auth). |

### 8.4 `/charge` flow (records metrics on EVERY outcome)
Wrap the body so a metrics entry (`latency_ms`, `ok`, `status_code`, `cause`, `db_ms`) is recorded in a `finally` for all return/exception paths.
1. Log `charge_received` (request_id, user_id, amount).
2. If `payment_failure` → log `charge_failed cause=payment_failure`, return **502** `{"error":"Payment gateway timeout"}`.
3. If `random_error_rate>0` and `random()<rate` → log `charge_failed cause=random_error`, return **500**.
4. If `memory_leak` on → append ~1 MB to the leak list, capped (§11.3).
5. DB work on one charge-pool connection (records `db_ms`): log `db_query_start`; if `db_slowdown>0` run `SELECT pg_sleep($1)`; INSERT (`status='success'`); verifying SELECT (a no-semantic read of the inserted row, purely to add realistic DB time; its result is not validated); log `db_query_done duration_ms`.
6. On any DB exception (pool timeout / query error / DB down) → log `charge_failed cause=db_error` at ERROR, return **503**.
7. Success → log `charge_success`, return **200** `{"status":"success","transaction_id":<id>}`.

> **Ordering caveat for combined modes:** steps 2–3 short-circuit before steps 4–5, so enabling `random_error_rate=1.0` or `payment_failure` hides the memory-leak/slow-query evidence. The demo (§12) therefore resets between scenarios. (Do not combine `random_error_rate=1.0` with `memory_leak`/`db_slowdown` and expect all three to show.)

### 8.5 Chaos flags (payment-service only)
Valid set: `db_slowdown`, `random_error_rate`, `memory_leak`, `payment_failure`. Any other flag → 400.

| Flag | Type | Default | Effect |
|---|---|---|---|
| `db_slowdown` | float sec, clamp `[0,DB_SLOWDOWN_MAX]` | `0` | `SELECT pg_sleep(value)` in `/charge`; real slow-query time |
| `random_error_rate` | float `[0,1]` | `0` | probability `/charge` returns 500 |
| `memory_leak` | bool | `false` | append ~1 MB/req, hard-capped; cleared on reset |
| `payment_failure` | bool | `false` | `/charge` returns 502 "Payment gateway timeout" |

### 8.6 `/status` shape
```json
{
  "service": "payment-service",
  "window_sec": 60,
  "requests": 142, "errors": 0, "error_rate": 0.0,
  "latency_ms": { "p50": 12, "p95": 5200 },
  "db_query_ms": { "p50": 8, "p95": 5100 },
  "chaos": { "db_slowdown": 6.0, "random_error_rate": 0.0, "memory_leak": false, "payment_failure": false },
  "memory_leak_mb": 0,
  "recent": [ { "ts": "2026-06-27T12:35:02.800Z", "request_id": "ab12", "latency_ms": 6005, "ok": true, "status_code": 200, "db_ms": 6001, "cause": null } ]
}
```
Empty window: `requests=0, errors=0, error_rate=0.0, p50=p95=null`. `recent` = last N (default 20); `ts` uses the §7.1 ISO-8601-`Z` format.

### 8.7 Logging
Every `/charge` logs `charge_received`, `db_query_start`, `db_query_done duration_ms`, and the outcome (`charge_success` or `charge_failed cause=<...>`), all carrying `request_id`. Chaos flips log `chaos_set` at WARN.

---

## 9. checkout-service spec

### 9.1 Endpoints
| Method | Path | Behavior |
|---|---|---|
| POST | `/checkout` | Body `{user_id, amount}`. Generate `request_id`; (optional `own_latency`) then `POST {PAYMENT_SERVICE_URL}/charge` with `X-Request-Id` and a `PAYMENT_TIMEOUT_SECONDS` (default 3) timeout via a shared `httpx.AsyncClient`. Records metrics on every path. |
| GET | `/health` | Static `{"status":"ok"}` 200 (no DB). Exempt from chaos. |
| GET | `/status` | Rich evidence JSON (9.5). |
| POST | `/admin/chaos/{flag}/{value}` | Set a flag (auth). |
| POST | `/admin/chaos/reset` | Reset flags (auth). |

### 9.2 `/checkout` flow (records metrics on EVERY outcome; `finally`)
1. Generate `request_id`; log `checkout_received` (request_id, user_id, amount).
2. If `random_error_rate` roll hits → log `checkout_failed cause=random_error`, return **500**.
3. If `own_latency>0` → `await asyncio.sleep(value)`.
4. Log `payment_call_start`; `POST /charge` via the shared client with timeout.
5. **Result mapping** (httpx does NOT raise on 5xx — must inspect status):
   - `httpx.TimeoutException` → log `payment_call_result status=timeout elapsed_ms=..`, return **504** `{"error":"timeout calling payment-service"}`.
   - `httpx.HTTPError`/transport (e.g. `ConnectError`) → log `payment_call_result status=error code=conn_error`, return **502**.
   - response `status_code>=500` → log `payment_call_result status=error code=<status>`, return **502**.
   - else → log `payment_call_result status=ok`, `checkout_success`, return **200**.

### 9.3 Chaos flags (checkout-service only)
Valid set: `own_latency`, `random_error_rate`. Any other flag → 400.

| Flag | Type | Default | Effect |
|---|---|---|---|
| `own_latency` | float sec, clamp `[0,OWN_LATENCY_MAX]` | `0` | delay before calling payment (checkout itself slow) |
| `random_error_rate` | float `[0,1]` | `0` | random 500s, independent of payment health |

### 9.4 The intentionally-misleading log + correlation
During a payment `db_slowdown` incident, checkout logs `payment_call_result status=timeout` **without knowing why**. The real cause is visible only in payment-service. To give phase 2 a ground-truth join (not just temporal correlation), checkout generates a `request_id` per `/checkout`, sends it as `X-Request-Id` to `/charge`, and both services log it and include it in `/status.recent[]`. Phase 2's AI can correlate statistically (timing) AND verify via `request_id`.

### 9.5 `/status` shape (checkout)
Same as 8.6 but **without** `db_query_ms` and **without** `memory_leak_mb`, and `chaos` contains exactly `{own_latency, random_error_rate}`:
```json
{
  "service": "checkout-service",
  "window_sec": 60,
  "requests": 140, "errors": 38, "error_rate": 0.271,
  "latency_ms": { "p50": 30, "p95": 3001 },
  "chaos": { "own_latency": 0.0, "random_error_rate": 0.0 },
  "recent": [ { "ts": "2026-06-27T12:35:08.100Z", "request_id": "cd34", "latency_ms": 3001, "ok": false, "status_code": 504, "cause": "payment_timeout" } ]
}
```

---

## 10. loadgen worker spec

- **Open-loop scheduler:** fires a `POST {CHECKOUT_SERVICE_URL}/checkout` every `~1/LOADGEN_RPS` seconds (± jitter) as an **independent task** (`asyncio.create_task` per tick or a token-bucket), so slow in-flight requests during an incident do **not** throttle new ones — request volume stays ~constant on the graphs/`/status`. Cap concurrent in-flight at `LOADGEN_MAX_INFLIGHT` (default 50) to avoid unbounded growth; if at cap, skip the tick and count it.
- Uses one shared `httpx.AsyncClient` with `LOADGEN_TIMEOUT_SECONDS` (default 20, **must exceed** `OWN_LATENCY_MAX + PAYMENT_TIMEOUT`) so even slow/timed-out checkouts are still recorded as traffic.
- Logs sparsely: a one-line summary every ~10 s (sent/ok/failed/inflight). No chaos, no admin endpoints, not in the chaos env group. Keeps both web services warm.

---

## 11. Correctness & safety decisions

### 11.1 `/health` is isolated from chaos in CODE and in RESOURCES
`/health` does only a fast `SELECT 1` via a **dedicated health pool** (§8.2) and is exempt from every chaos flag. Because it never draws from the charge pool, a fully-saturated charge pool (from `pg_sleep`) cannot block it. Combined with the `db_slowdown` clamp (§11.2) and the memory hard-cap (§11.3), a chaos run cannot make `/health` exceed Render's 5 s success window — so Render will not stop routing (~15 s) or restart (~60 s) the instance mid-demo and wipe in-memory state. *This is the single most important invariant; it has a dedicated DoD check.*

### 11.2 `db_slowdown` is clamped so the charge pool can't exhaust
A held connection lasts the full `db_slowdown` (checkout's 3 s timeout does NOT free it — uvicorn doesn't cancel the handler on client disconnect). Expected concurrency ≈ `LOADGEN_RPS × db_slowdown`. With `LOADGEN_RPS=2` and `DB_SLOWDOWN_MAX=12`, peak ≈ 24 < charge pool 30, leaving headroom (and `/health` is on its own pool). `db_slowdown` is **server-clamped** to `DB_SLOWDOWN_MAX_SECONDS`; `DB_STATEMENT_TIMEOUT_MS` is a backstop. So the story stays a clean "slow query," never an accidental pool-exhaustion cascade — even when the presenter "dials for drama." (To allow larger slowdowns, raise `DB_POOL_MAX` and `DB_SLOWDOWN_MAX` together, keeping `rps×max < pool`.)

### 11.3 Memory leak: visible, hard-capped, honest about reset
Append ~1 MB chunks (a few large `bytearray`s) per `/charge` while enabled, **hard-capped in code** at `min(MEMORY_LEAK_CAP_MB, 250)` regardless of env, so a 512 MB Starter instance can't OOM (peak ~300 MB over ~100 MB baseline). `reset` clears the list so `memory_leak_mb` returns 0 and growth stops — but **CPython may not return freed memory to the OS**, so Render's RSS graph may plateau rather than visibly drop. The demo/DoD does not promise a visible memory decrease on reset.

### 11.4 Single instance, single worker (in-memory state)
All chaos + metrics state is per-process. `numInstances: 1` (no autoscaling) and `uvicorn --workers 1` are **required** on both web services. Scaling >1 instance/worker would split state across processes — a chaos POST would hit one process while loadgen/`/status` hit another, making toggles appear not to work and `/status` inconsistent, and would break the independence demo (§11.4). Any Render restart (deploy/OOM/platform) wipes state — avoid redeploys mid-demo.

### 11.5 DECISION (resolved) — internal URLs (Option A)
**Chosen: Option A — internal URLs for both HTTP hops** (`loadgen→checkout`, `checkout→payment`), as wired in §6 via `fromService hostport`. The demo's metric surface is the rich `/status` JSON (richer than Render's graphs and already the phase-2 source), plus Render's CPU/memory graphs (memory clearly moves under `memory_leak`).

Render's HTTP request/latency/error graphs are measured at the edge router and **likely do not reflect private-network (internal) traffic**, so those particular graphs may stay flat during an incident — an **accepted trade-off**, because `/status` fully captures the incident and Option B (external URLs) would forfeit one-click Blueprint auto-wiring (`fromService` can't supply a public URL) and add a public hop. The DB connection is internal regardless.

---

## 12. Demo script (hero scenario — reset between modes)

```bash
KEY=<CHAOS_ADMIN_KEY from Render env group>
PAY=https://payment-service-xxxx.onrender.com
CHK=https://checkout-service-xxxx.onrender.com

# 0. Baseline (loadgen running): /status healthy, graphs steady volume
curl $PAY/status; curl $CHK/status

# 1. Hero: 6s DB slowdown (> checkout's 3s timeout)
curl -X POST $PAY/admin/chaos/db_slowdown/6 -H "X-Chaos-Key: $KEY"
curl $CHK/status     # checkout: error_rate up, "timeout calling payment-service"
curl $PAY/status     # payment: db_query_ms.p95 ~6000 — the real cause; /health still ok
curl -X POST $PAY/admin/chaos/reset -H "X-Chaos-Key: $KEY"     # RESET before next mode

# 2. Clean 500s
curl -X POST $PAY/admin/chaos/random_error_rate/1.0 -H "X-Chaos-Key: $KEY"
curl $PAY/status     # errors/error_rate climb
curl -X POST $PAY/admin/chaos/reset -H "X-Chaos-Key: $KEY"     # RESET

# 3. Memory leak
curl -X POST $PAY/admin/chaos/memory_leak/true -H "X-Chaos-Key: $KEY"
# watch Render memory graph trend up; memory_leak_mb in /status rises
curl -X POST $PAY/admin/chaos/reset -H "X-Chaos-Key: $KEY"     # growth stops (RSS may plateau, not drop)

# 4. Independence: checkout's own errors don't touch payment
curl -X POST $CHK/admin/chaos/random_error_rate/0.5 -H "X-Chaos-Key: $KEY"
curl $PAY/status     # payment unaffected
curl -X POST $CHK/admin/chaos/reset -H "X-Chaos-Key: $KEY"
```

---

## 13. Validation checklist (Definition of Done)

- [ ] Blueprint deploys all 4 components; both web services reach `/health` 200; loadgen is sending traffic.
- [ ] `/status` returns 200 well-formed JSON **with zero traffic** (empty-window guards work).
- [ ] Baseline: `/checkout` succeeds fast; both `/status` low latency, 0 errors; request volume steady & non-zero.
- [ ] `db_slowdown/6` →
  - [ ] payment logs `db_query_done duration_ms≈6000`; payment `/status` `db_query_ms.p95≈6000`.
  - [ ] checkout logs `payment_call_result status=timeout`, **not** a DB error; checkout `/status` error_rate rises.
  - [ ] **payment-service is NOT restarted** and `/health` p95 stays <200 ms — at the demo value AND at `2×` it (proves §11.1).
  - [ ] request volume stays ~constant (within jitter) during the incident — only error/latency change (proves open-loop loadgen).
- [ ] `random_error_rate/1.0` on payment → `/status` errors/error_rate climb; clean 500s in logs.
- [ ] `memory_leak/true` → Render memory graph trends up and **caps without crashing**; `memory_leak_mb` rises; `reset` stops growth (no promise of a visible drop).
- [ ] checkout `random_error_rate` independently → payment `/status` unaffected (independence, §11.4).
- [ ] Tunable + clamp: `db_slowdown/2.5` intermittent; `/6` consistent; `db_slowdown/9999` is clamped to `DB_SLOWDOWN_MAX` (echoed in response); `random_error_rate/2.0` clamped to 1.0; `db_slowdown/abc` → 400.
- [ ] Per-service flag validation: POST `memory_leak` to checkout → 400; `own_latency` to payment → 400.
- [ ] `X-Chaos-Key` required: unauthenticated `/admin/chaos/*` → 401.
- [ ] `request_id` appears in both services' logs and `/status.recent[]` for the same logical request.
- [ ] `/status` JSON well-formed & phase-2-parseable on both services (checkout schema per §9.5).
- [ ] §11.5 decision recorded (internal vs external URLs) and reflected in `render.yaml` + demo URLs.

---

## 14. Render Blueprint verification appendix — RESOLVED

All §6 field names were verified against the current Render Blueprint spec (2026-06). **No field changes were required.**

- [x] `databases[].plan = basic-256mb` — smallest paid Postgres; **`max_connections = 100`** for 256 MB (any instance <8 GB RAM). Charge pool 30 + health pool 2 ≪ 100 (§8.2 safe).
- [x] `services[].runtime: python` is current; legacy `env: python` is discouraged.
- [x] `fromDatabase.property: connectionString` = **internal** connection string (same-region; default region keeps it internal). *Verify at impl time whether asyncpg needs `ssl=`; the internal URL typically does not, but fail loudly with a clear log if `DATABASE_URL` looks external.*
- [x] `fromService.property: hostport` returns internal `host:port`; **no full-URL property exists**, so code prepends `http://` (internal traffic is plain HTTP).
- [x] Shared secret: `envVarGroups` + `generateValue: true` generates **one** value, shared identically via `- fromGroup`. (Per-service `generateValue` would differ — would break `X-Chaos-Key`.)
- [x] `type: worker` requires a **paid** plan (`starter`); workers have no free tier.
- [x] Starter web = **512 MB / 0.5 CPU** (informs the 250 MB hard memory cap). Health-check: success = 2xx/3xx within 5 s; ~15 s consecutive failures → stop routing; ~60 s → **restart** (validates §11.1).
- [x] §11.5 internal-vs-external URL decision — **RESOLVED: Option A (internal URLs)**; `render.yaml` already wired internal via `fromService hostport`.

---

## 15. Phase-2 readiness notes (context only — do not build)

- Phase 2 pulls both services' `/status` as the **primary, reliable** evidence source (richer than Render's graphs), and may additionally ingest Render logs/metrics for narrative.
- Phase-1 outputs are designed for a machine consumer: `/status` is structured, fully specified (incl. empty-window behavior and a pinned checkout schema), and timestamps match the logs' ISO-8601-`Z` format.
- The correlation challenge is built in but solvable: checkout's logs are misleading in isolation; root cause requires joining checkout + payment evidence, with `request_id` as ground truth.
