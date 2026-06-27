# Checkout Cascade — Phase 1 Design Spec

**Date:** 2026-06-27
**Status:** Approved design (pre-implementation)
**Scope:** Phase 1 only — build and deploy two faulty services + a load generator that produce believable, organic incident data on Render. Phase 2 (SuperPlane incident-AI pipeline) is out of scope and noted only where it shapes phase-1 outputs.

---

## 1. Summary

A chaos-engineering demo on Render that tells a realistic incident story: **checkout looks broken, but the real root cause is a degraded database call inside payment-service.**

- `checkout-service` (web) calls `payment-service` (web) over HTTP.
- `payment-service` is the only service with database access; it reads/writes Postgres.
- A `loadgen` background worker drives continuous, jittered traffic so Render's native metrics show a real baseline and a real incident trend.
- Both web services expose `/admin/chaos/*` endpoints to inject failure modes live, with **tunable magnitude**, without redeploying.
- Both services emit a **rich `/status` JSON** (the reliable phase-2 evidence source) and **structured, grep-able logs** (corroborating real telemetry).

Phase 2 (not built here) will connect Render's logs/metrics + these services' `/status` to SuperPlane, which assembles an evidence pack and triggers an AI-generated incident summary to Slack.

---

## 2. Goals and non-goals

### Goals
- Four independently deployable Render components (2 web + 1 worker + 1 Postgres), provisioned by a single Blueprint (`render.yaml`).
- `checkout-service` → calls → `payment-service` → reads/writes → Postgres.
- Chaos toggles on both web services, controllable via HTTP without redeploying, with **tunable magnitude**.
- A load generator producing steady, jittered baseline traffic so Render metrics (latency, error rate, CPU, memory) visibly reflect injected chaos.
- Clean, readable, grep-able logs on both services that diverge meaningfully during an incident.
- Rich `/status` JSON on both services as the primary, reliable evidence interface for phase 2.
- Deployable today on paid Starter tier, demoable live (dial chaos in front of judges).

### Non-goals (do not build in this phase)
- SuperPlane integration (phase 2).
- Slack integration (phase 2).
- The AI reasoning/root-cause layer (phase 2).
- Redis / centralized chaos state (chaos state is in-memory per service, by design).
- Authentication/user accounts (internal demo tool).
- Frontend UI (curl/Postman/`/status` JSON is enough).
- Persisting metrics beyond an in-memory rolling window.

---

## 3. Architecture

```
┌──────────────────────────── Render ────────────────────────────┐
│                                                                 │
│   loadgen (worker) ──POST /checkout (internal)──▶ checkout-svc  │
│        steady ~2 req/s + jitter                       │ (web)   │
│                                                       │         │
│                                  POST /charge (internal, 3s TO) │
│                                                       ▼         │
│                                               payment-service   │
│                                                   (web)         │
│                                                       │         │
│                                          asyncpg (internal URL) │
│                                                       ▼         │
│                                          checkout-cascade-db    │
│                                            (Postgres, Basic)    │
└─────────────────────────────────────────────────────────────────┘
```

- `checkout-service` has **no DB access**. It only knows `payment-service`'s HTTP API.
- `payment-service` is the **only** holder of `DATABASE_URL`.
- All inter-service calls use Render **internal** URLs (private network, lower latency).
- Both web services log to stdout/stderr (captured natively by Render) and expose `/admin/chaos/*`, `/status`, `/health`.
- `loadgen` keeps both web services warm (no cold starts mid-demo) and generates the traffic the metric graphs need.

### Data flow (happy path)
`loadgen` → `POST /checkout {user_id, amount}` → checkout logs + (optional own_latency) → `POST payment/charge` (3 s timeout) → payment logs `charge_received` → INSERT transaction → verifying SELECT → respond success → checkout logs `checkout_success` → returns 200.

### Data flow (hero incident: `db_slowdown`)
Operator sets `db_slowdown=6` on payment → every `/charge` runs `SELECT pg_sleep(6)` as a real slow query → payment `/status` p95 and `db_query_ms` spike, logs show `db_query_done duration_ms=6005` → checkout's 3 s call times out → checkout logs `payment_call_result status=timeout` (it does **not** know about the DB) → checkout `/status` error_rate climbs → Render latency/error graphs on payment rise; checkout shows timeouts. The root cause is visible **only** in payment-service.

---

## 4. Tech stack & key decisions

| Choice | Decision | Rationale |
|---|---|---|
| Language / framework | Python 3.11+ / FastAPI + uvicorn | fast to build, async-friendly for simulated latency |
| DB driver | raw `asyncpg` with a connection pool | simplest; pool needed so `pg_sleep` doesn't exhaust connections |
| Runtime | Render **native** Python runtime (no Dockerfile) | asyncpg needs no OS packages; simpler builds |
| Deploy | single Render **Blueprint** (`render.yaml`) | reproducible; auto-wires internal URLs + shared secret; one-click |
| Tier | **paid Starter** (web + worker), **Basic** Postgres | background workers are paid-only; paid web avoids sleep/cold-start |
| `db_slowdown` impl | **SQL-level `pg_sleep(value)`** | shows as genuine slow-query time in DB + app metrics (more authentic than `asyncio.sleep`) |
| Chaos magnitude | **tunable** via the value path segment | one deterministic value for the judged run; dial for drama or intermittency |
| Chaos state | in-memory per service | independence is a goal; single instance per service for the demo |
| Evidence source | **rich `/status` JSON (primary) + structured logs (corroborating)** | `/status` pull is reliable for phase 2; Render log/metric APIs are fragile/rate-limited |

---

## 5. Repository structure

Monorepo, new git repo at `/Users/yash/Documents/checkout-cascade`. Services are **self-contained** (no cross-folder imports) so each Render service's root-directory build is trivial. The small `metrics`/`logging`/`chaos` helpers are duplicated per service intentionally — it keeps deploys simple and reinforces the independent-chaos-state goal.

```
checkout-cascade/
├── render.yaml                     # Blueprint: 2 web + 1 worker + 1 db, all env wiring
├── README.md                       # endpoints, chaos flags, demo curl script
├── .gitignore
├── docs/superpowers/specs/2026-06-27-checkout-cascade-design.md   # this file
├── loadgen/
│   ├── run.py                      # async loop: POST /checkout at ~2 rps + jitter
│   └── requirements.txt
├── checkout-service/
│   ├── main.py                     # FastAPI app, endpoints
│   ├── chaos.py                    # chaos state + flag parsing
│   ├── metrics.py                  # rolling-window ring buffer + percentiles
│   ├── logging_setup.py            # structured one-line logger
│   └── requirements.txt
└── payment-service/
    ├── main.py
    ├── chaos.py
    ├── metrics.py
    ├── logging_setup.py
    ├── db.py                       # asyncpg pool, startup migration, /charge queries
    └── requirements.txt
```

---

## 6. Render deployment — Blueprint

A single `render.yaml` provisions everything and wires env vars so there is no manual dashboard juggling. `CHAOS_ADMIN_KEY` is a generated secret shared across all three services via an environment group; service URLs are injected via `fromService`; the DB URL via `fromDatabase`.

> **Note:** the exact Render Blueprint field names below (plan identifiers, `fromService.property`, `fromDatabase.property`, env-group sharing, `runtime` vs `env`) are validated in an appendix verification pass (§14). Treat §6 as the intended topology; the verification appendix records any corrections applied.

```yaml
# render.yaml (intended shape — verified in §14)
databases:
  - name: checkout-cascade-db
    plan: basic-256mb            # smallest paid Postgres (verify current id)
    databaseName: checkout_cascade
    user: checkout_cascade

envVarGroups:
  - name: chaos-shared
    envVars:
      - key: CHAOS_ADMIN_KEY
        generateValue: true      # one generated secret, shared by all services

services:
  - type: web
    name: payment-service
    runtime: python
    plan: starter
    rootDir: payment-service
    buildCommand: pip install -r requirements.txt
    startCommand: uvicorn main:app --host 0.0.0.0 --port $PORT
    healthCheckPath: /health
    envVars:
      - fromGroup: chaos-shared
      - key: DATABASE_URL
        fromDatabase:
          name: checkout-cascade-db
          property: connectionString   # internal connection string
      - key: DB_POOL_MAX
        value: "20"
      - key: MEMORY_LEAK_CAP_MB
        value: "200"

  - type: web
    name: checkout-service
    runtime: python
    plan: starter
    rootDir: checkout-service
    buildCommand: pip install -r requirements.txt
    startCommand: uvicorn main:app --host 0.0.0.0 --port $PORT
    healthCheckPath: /health
    envVars:
      - fromGroup: chaos-shared
      - key: PAYMENT_SERVICE_URL          # internal host:port; code prepends http://
        fromService:
          name: payment-service
          type: web
          property: hostport
      - key: PAYMENT_TIMEOUT_SECONDS
        value: "3"

  - type: worker
    name: loadgen
    runtime: python
    plan: starter
    rootDir: loadgen
    buildCommand: pip install -r requirements.txt
    startCommand: python run.py
    envVars:
      - key: CHECKOUT_SERVICE_URL         # internal host:port; code prepends http://
        fromService:
          name: checkout-service
          type: web
          property: hostport
      - key: LOADGEN_RPS
        value: "2"
```

**Manual fallback** (if not using the Blueprint): create the Postgres, then payment-service (set `DATABASE_URL` from the internal connection string, generate a `CHAOS_ADMIN_KEY`), then checkout-service (set `PAYMENT_SERVICE_URL` to payment's internal URL, same `CHAOS_ADMIN_KEY`), then the loadgen worker (set `CHECKOUT_SERVICE_URL`). The Blueprint is strongly preferred.

---

## 7. Shared conventions

### 7.1 Structured logging
One line per event, leading ISO-8601 UTC timestamp, level, service, event, then `key=value` fields. Grep-able and parse-friendly.

```
2026-06-27T12:34:56.789Z INFO  payment-service event=charge_received user_id=demo-user amount=49.99
2026-06-27T12:34:56.794Z INFO  payment-service event=db_query_start
2026-06-27T12:35:02.799Z INFO  payment-service event=db_query_done duration_ms=6005
2026-06-27T12:35:02.800Z INFO  payment-service event=charge_success
2026-06-27T12:35:10.000Z WARN  payment-service event=chaos_set flag=db_slowdown value=6.0
2026-06-27T12:34:59.900Z WARN  checkout-service event=payment_call_result status=timeout elapsed_ms=3001
```
Chaos flips always log at `WARN` with the `chaos_set` event so they're easy to locate. Errors log at `ERROR` with a `cause=` field naming the responsible chaos flag.

### 7.2 Rolling-window metrics (ring buffer)
Each service keeps an in-memory list of recent request records, each `{ts, latency_ms, ok, status_code, cause?, db_ms?}`. Records older than `window_sec` (default 60) are pruned on read. Percentiles (p50/p95) computed by sorting the window. This powers `/status`. It is **not** persisted (acceptable — phase 2 polls live).

### 7.3 Chaos admin auth
All `/admin/chaos/*` endpoints require header `X-Chaos-Key: <CHAOS_ADMIN_KEY>`. Missing/wrong key → `401`. `/charge`, `/checkout`, `/health`, `/status` are unauthenticated.

### 7.4 Chaos value parsing
`POST /admin/chaos/{flag}/{value}`: `value` is parsed as float when the flag is magnitude-typed (`db_slowdown`, `random_error_rate`, `own_latency`) and as bool (`true/false/1/0`) when boolean-typed (`memory_leak`, `payment_failure`). Unknown flag → `400`. `POST /admin/chaos/reset` zeroes/false-es all flags and frees leaked memory.

---

## 8. payment-service spec

### 8.1 Database schema (startup migration)
Executed once on app startup (`CREATE TABLE IF NOT EXISTS`, no migration framework):
```sql
CREATE TABLE IF NOT EXISTS transactions (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    amount NUMERIC(10,2) NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### 8.2 asyncpg pool
- Pool created on startup: `min_size=2`, `max_size=DB_POOL_MAX` (default 20).
- **`max_size` must stay below the Postgres tier's `max_connections`** (verify the Basic-tier limit in §14). With loadgen at ~2 rps and a 6 s `pg_sleep`, expected concurrency ≈ 12 (Little's law), so pool 20 has headroom.
- Pool sized this way so `db_slowdown` reads as a clean "slow query," not an accidental connection-pool-exhaustion cascade.

### 8.3 Endpoints
| Method | Path | Behavior |
|---|---|---|
| POST | `/charge` | Body `{user_id, amount}`. Apply chaos (see 8.5), INSERT a row, run verifying SELECT, return `{status, transaction_id}`. The only chaos-injected business endpoint. |
| GET | `/health` | Fast `SELECT 1` only. `{"status":"ok"}` (200) if DB reachable, else 503. **Exempt from all chaos** (see §11.1). |
| GET | `/status` | Rich evidence JSON (see 8.6). Unauthenticated. |
| POST | `/admin/chaos/{flag}/{value}` | Set a flag (auth required). |
| POST | `/admin/chaos/reset` | Reset all flags + free leaked memory (auth required). |

### 8.4 `/charge` flow (order matters)
1. Log `charge_received` (user_id, amount).
2. If `payment_failure` on → log `charge_failed cause=payment_failure`, return **502** `{"error":"Payment gateway timeout"}`.
3. If `random_error_rate > 0` and `random() < rate` → log `charge_failed cause=random_error`, return **500**.
4. If `memory_leak` on → append ~1 MB to the module-level leak list, capped at `MEMORY_LEAK_CAP_MB` (default 200).
5. DB work (records `db_ms`): log `db_query_start`; if `db_slowdown > 0` run `SELECT pg_sleep($1)` with the configured seconds; INSERT the transaction (`status='success'`); run a verifying `SELECT`; log `db_query_done duration_ms=...`.
6. Record metrics, log `charge_success`, return **200** `{"status":"success","transaction_id":<id>}`.

### 8.5 Chaos flags
| Flag | Type | Default | Effect |
|---|---|---|---|
| `db_slowdown` | float (seconds) | `0` | `SELECT pg_sleep(value)` inside `/charge`; real slow-query time |
| `random_error_rate` | float `0.0–1.0` | `0` | probability `/charge` returns a 500 |
| `memory_leak` | bool | `false` | append ~1 MB/req to an in-memory list, capped at `MEMORY_LEAK_CAP_MB`; freed on reset |
| `payment_failure` | bool | `false` | `/charge` returns a clean 502 "Payment gateway timeout" |

### 8.6 `/status` shape
```json
{
  "service": "payment-service",
  "window_sec": 60,
  "requests": 142,
  "errors": 0,
  "error_rate": 0.0,
  "latency_ms": { "p50": 12, "p95": 5200 },
  "db_query_ms": { "p50": 8, "p95": 5100 },
  "chaos": { "db_slowdown": 6.0, "random_error_rate": 0.0, "memory_leak": false, "payment_failure": false },
  "memory_leak_mb": 0,
  "recent": [ { "ts": "...", "latency_ms": 6005, "ok": true, "status_code": 200, "db_ms": 6001, "cause": null } ]
}
```
`recent` holds the last N (default 20) records. `db_query_ms` is payment-only.

### 8.7 Logging requirements
Every `/charge` logs: `charge_received`, `db_query_start`, `db_query_done duration_ms`, and the outcome (`charge_success` or `charge_failed cause=<flag>`). Chaos flips log `chaos_set flag=.. value=..` at WARN.

---

## 9. checkout-service spec

### 9.1 Endpoints
| Method | Path | Behavior |
|---|---|---|
| POST | `/checkout` | Body `{user_id, amount}`. (optional `own_latency` delay) then `POST {PAYMENT_SERVICE_URL}/charge` with a `PAYMENT_TIMEOUT_SECONDS` (default 3) timeout. Return success, or a clear timeout/error message. |
| GET | `/health` | `{"status":"ok"}` (200). No DB; exempt from chaos. |
| GET | `/status` | Rich evidence JSON (same shape as 8.6, minus `db_query_ms`). |
| POST | `/admin/chaos/{flag}/{value}` | Set a flag (auth required). |
| POST | `/admin/chaos/reset` | Reset all flags (auth required). |

### 9.2 `/checkout` flow
1. Log `checkout_received` (user_id, amount).
2. If `random_error_rate` roll hits → log `checkout_failed cause=random_error`, return 500.
3. If `own_latency > 0` → `await asyncio.sleep(value)` (checkout being slow itself, independent of payment).
4. Log `payment_call_start`; `POST /charge` via httpx with timeout.
5. On success → log `payment_call_result status=ok`, `checkout_success`, return 200.
6. On timeout → log `payment_call_result status=timeout elapsed_ms=..`, return 504 `{"error":"timeout calling payment-service"}`.
7. On HTTP error (5xx/connection) → log `payment_call_result status=error code=..`, return 502.

### 9.3 Chaos flags
| Flag | Type | Default | Effect |
|---|---|---|---|
| `own_latency` | float (seconds) | `0` | delay before calling payment-service (checkout itself slow) |
| `random_error_rate` | float `0.0–1.0` | `0` | random 500s, independent of payment health |

### 9.4 The intentionally-misleading log
During a payment `db_slowdown` incident, checkout logs `payment_call_result status=timeout` **without knowing why** payment was slow. That is the point: the real cause (slow DB query) is visible only in payment-service's logs/`/status`. Phase 2's AI must correlate the two services to find root cause.

---

## 10. loadgen worker spec

- Async loop: `POST {CHECKOUT_SERVICE_URL}/checkout` with a small random payload (`user_id`, `amount`), targeting `LOADGEN_RPS` (default 2) with ±jitter so traffic looks organic.
- Uses httpx with its own generous timeout (longer than checkout's) so loadgen records both fast and timed-out checkouts as traffic (keeping request volume visible during incidents).
- Logs sparsely (e.g. a one-line summary every ~10 s: sent/ok/failed counts) to avoid drowning the services' own logs.
- No chaos, no admin endpoints. Pure traffic source. Keeps both web services warm.

---

## 11. Correctness & safety decisions

### 11.1 Chaos must never affect `/health` (most important)
`/health` performs only a fast `SELECT 1` (payment) or returns static OK (checkout) and is **exempt from every chaos flag**. If a slowdown/error/leak made the health check fail, Render would restart the service mid-demo and wipe the incident + in-memory chaos/metrics state. Chaos lives only in `/charge` and `/checkout`.

### 11.2 Pool sized above injected concurrency
`db_slowdown` holds a connection for its duration. The pool (`max_size=20`) is sized above expected concurrency so the demo stays a clean "slow query," not a pool-exhaustion cascade — while staying under the Postgres tier connection limit (§14).

### 11.3 Memory leak: visible but safe
Default cap `MEMORY_LEAK_CAP_MB=200` on a 512 MB Starter instance → memory trends from ~100 MB baseline to ~300 MB (clearly visible on Render's graph) with safe headroom against OOM. Tunable via env; keep well under ~400 MB on a 512 MB instance. (This raises the original plan's 50 MB cap, which would be too subtle on a 512 MB instance.) Freed on `reset`.

### 11.4 Independence is explicit
checkout and payment each own separate chaos state and ring buffers. Toggling checkout's `random_error_rate` provably does not affect payment, and vice versa — demonstrated in the validation checklist.

---

## 12. Demo script (hero scenario)

```bash
KEY=<CHAOS_ADMIN_KEY from Render env group>
PAY=https://payment-service-xxxx.onrender.com
CHK=https://checkout-service-xxxx.onrender.com

# 0. Baseline (loadgen already running): graphs flat & green, /status healthy
curl $PAY/status; curl $CHK/status

# 1. Inject the cascade: 6s DB slowdown (> checkout's 3s timeout)
curl -X POST $PAY/admin/chaos/db_slowdown/6 -H "X-Chaos-Key: $KEY"
#   -> payment latency/db_query_ms p95 spike; checkout starts timing out
#   -> Render payment graph: latency up; checkout graph: errors up

# 2. Show the divergence
curl $CHK/status     # checkout: error_rate up, "timeout calling payment-service"
curl $PAY/status     # payment: db_query_ms.p95 ~6000, the real cause

# 3. Other modes (optional)
curl -X POST $PAY/admin/chaos/random_error_rate/1.0 -H "X-Chaos-Key: $KEY"
curl -X POST $PAY/admin/chaos/memory_leak/true      -H "X-Chaos-Key: $KEY"

# 4. Reset everything
curl -X POST $PAY/admin/chaos/reset -H "X-Chaos-Key: $KEY"
curl -X POST $CHK/admin/chaos/reset -H "X-Chaos-Key: $KEY"
```

---

## 13. Validation checklist (Definition of Done)

- [ ] Blueprint deploys all 4 components; both web services reach `/health` 200; loadgen is sending traffic.
- [ ] Baseline: `/checkout` succeeds consistently and fast; both `/status` show low latency, 0 errors; Render graphs show steady non-zero request volume (proves loadgen works).
- [ ] `db_slowdown/6` →
  - [ ] payment logs show `db_query_done duration_ms≈6000`; payment `/status` `db_query_ms.p95≈6000`.
  - [ ] checkout logs `payment_call_result status=timeout`, **not** a DB error; checkout `/status` error_rate rises.
  - [ ] Render metrics: payment latency rises; checkout error rate rises.
  - [ ] **payment-service is NOT restarted** (health stays green throughout) — confirms §11.1.
- [ ] `random_error_rate/1.0` on payment → clean 500s in logs + Render error-rate metric.
- [ ] `memory_leak/true` on payment → Render memory graph trends up and **caps without crashing**.
- [ ] checkout `random_error_rate` independently → payment unaffected (proves independence, §11.4).
- [ ] Tunable magnitude works: `db_slowdown/2.5` produces intermittent timeouts; `/6` produces consistent timeouts.
- [ ] `reset` on both returns everything to baseline (and frees leaked memory).
- [ ] `X-Chaos-Key` required: unauthenticated `/admin/chaos/*` → 401.
- [ ] `/status` JSON is well-formed and phase-2-parseable on both services.

---

## 14. Render Blueprint verification appendix

The Render-specific field names in §6 are validated against the current Render Blueprint spec before implementation. Corrections (if any) are recorded here:

- [ ] `databases[].plan` smallest paid Postgres identifier (e.g. `basic-256mb`) — confirm + note `max_connections` for that tier (informs §8.2 pool ceiling).
- [ ] `services[].runtime: python` vs legacy `env: python`.
- [ ] `fromDatabase.property` for the internal connection string (`connectionString`).
- [ ] `fromService.property: hostport` returns internal `host:port` (and whether scheme must be prepended in code).
- [ ] Sharing a generated secret across services via `envVarGroups` + `fromGroup` (vs per-service `generateValue`, which generates distinct values).
- [ ] `type: worker` plan identifier for a paid background worker (`starter`).

*(This appendix is filled in by the verification pass; the implementation plan must not begin until it is resolved.)*

---

## 15. Phase-2 readiness notes (context only — do not build)

- Phase 2 will pull both services' `/status` as the **primary, reliable** evidence source, and may additionally ingest Render logs/metrics for "real telemetry" narrative.
- Therefore phase-1 outputs are designed for a machine consumer: `/status` is structured and complete; logs are one-line, timestamped, and grep-able with explicit `event=` and `cause=` fields.
- The correlation challenge phase-2's AI must solve is deliberately built in: checkout's logs are misleading in isolation; root cause requires joining checkout + payment evidence.
