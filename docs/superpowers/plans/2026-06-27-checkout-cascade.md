# Checkout Cascade — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy two FastAPI services (`checkout-service` → `payment-service` → Postgres) plus a `loadgen` worker on Render that produce realistic, organic incident data via live, tunable chaos toggles, exposing rich `/status` JSON + structured logs.

**Architecture:** Self-contained services in a monorepo, deployed via one Render Blueprint (`render.yaml`) with internal URLs (Option A). Pure-Python helpers (`metrics`, `chaos`, `logging_setup`) are unit-tested first (TDD); FastAPI endpoints are tested with `TestClient` and stubbed DB/HTTP so no live Postgres is needed for the test loop. `payment-service` uses two asyncpg pools — a charge pool and a dedicated health pool — so chaos can never starve `/health`.

**Tech Stack:** Python 3.11+, FastAPI + uvicorn (`--workers 1`), asyncpg, httpx, pytest. Render (2 web + 1 worker + basic-256mb Postgres).

**Spec:** `docs/superpowers/specs/2026-06-27-checkout-cascade-design.md` (read §7–§11 before coding; they define every behavior below).

---

## Module API contract (names used consistently across all tasks)

**`metrics.py`** (identical file in both services):
- `class Metrics(window_sec=60, maxlen=600, recent_n=20)`
  - `record(latency_ms: float, ok: bool, status_code: int, cause: str | None = None, db_ms: float | None = None, request_id: str | None = None) -> None`
  - `snapshot(include_db: bool = True) -> dict` → `{window_sec, requests, errors, error_rate, latency_ms:{p50,p95}, [db_query_ms:{p50,p95}], recent:[...]}`
- `now_iso() -> str` → ISO-8601 UTC millisecond timestamp with trailing `Z`.

**`logging_setup.py`** (identical file in both services):
- `make_logger(service: str) -> callable` returning `log(event: str, level: str = "INFO", **fields) -> None`.

**`chaos.py`** (different per service):
- payment: `class Chaos(db_slowdown_max, statement_unused=None)` with state `db_slowdown, random_error_rate, memory_leak, payment_failure`.
- checkout: `class Chaos(own_latency_max)` with state `own_latency, random_error_rate`.
- both: `set(flag: str, value: str) -> float | bool` (raises `ChaosError(message)` on bad flag/parse/negative; clamps in range), `reset() -> None`, `as_dict() -> dict`.
- both expose `class ChaosError(Exception)` (endpoint maps to HTTP 400).

**`payment-service/db.py`**:
- `async def create_pools(dsn, pool_max, health_max, statement_timeout_ms) -> tuple[Pool, Pool]` (charge, health).
- `async def run_migration(charge_pool) -> None`.
- `async def run_charge(charge_pool, user_id, amount, db_slowdown, acquire_timeout) -> tuple[int, float]` → `(transaction_id, db_ms)`; raises on DB/pool error.
- `async def health_ping(health_pool, timeout) -> bool`.

---

## File Structure

```
checkout-cascade/
├── render.yaml                         # Task 11
├── README.md                           # Task 12
├── requirements-dev.txt                # Task 1
├── pytest.ini                          # Task 1 (root: collects all service tests)
├── payment-service/
│   ├── requirements.txt                # Task 1
│   ├── pytest.ini                      # Task 1
│   ├── logging_setup.py                # Task 4
│   ├── metrics.py                      # Task 2
│   ├── chaos.py                        # Task 3
│   ├── db.py                           # Task 5
│   ├── main.py                         # Task 6
│   └── tests/
│       ├── test_metrics.py             # Task 2
│       ├── test_chaos.py               # Task 3
│       ├── test_logging.py             # Task 4
│       └── test_main.py                # Task 6
├── checkout-service/
│   ├── requirements.txt                # Task 1
│   ├── pytest.ini                      # Task 7
│   ├── logging_setup.py                # Task 7 (identical to payment)
│   ├── metrics.py                      # Task 7 (identical to payment)
│   ├── chaos.py                        # Task 8
│   ├── main.py                         # Task 9
│   └── tests/
│       ├── test_chaos.py               # Task 8
│       └── test_main.py                # Task 9
└── loadgen/
    ├── requirements.txt                # Task 1
    ├── run.py                          # Task 10
    └── tests/test_scheduler.py         # Task 10
```

---

## Task 1: Scaffolding, dependencies, test tooling

**Files:**
- Create: `requirements-dev.txt`, `pytest.ini`, `payment-service/requirements.txt`, `payment-service/pytest.ini`, `checkout-service/requirements.txt`, `loadgen/requirements.txt`
- Create dirs: `payment-service/tests/`, `checkout-service/tests/`, `loadgen/tests/`

- [ ] **Step 1: Create requirements files**

`payment-service/requirements.txt`:
```
fastapi==0.115.*
uvicorn[standard]==0.32.*
asyncpg==0.30.*
```

`checkout-service/requirements.txt`:
```
fastapi==0.115.*
uvicorn[standard]==0.32.*
httpx==0.27.*
```

`loadgen/requirements.txt`:
```
httpx==0.27.*
```

`requirements-dev.txt`:
```
-r payment-service/requirements.txt
-r checkout-service/requirements.txt
pytest==8.*
pytest-asyncio==0.24.*
httpx==0.27.*
pyyaml==6.*
```

- [ ] **Step 2: Create pytest config so service folders are importable**

`pytest.ini` (repo root — runs all suites with each service dir on the path):
```ini
[pytest]
addopts = -q
pythonpath = payment-service checkout-service loadgen
testpaths = payment-service/tests checkout-service/tests loadgen/tests
asyncio_mode = auto
```

`payment-service/pytest.ini` (so you can also run the suite from inside the folder):
```ini
[pytest]
addopts = -q
pythonpath = .
testpaths = tests
```

- [ ] **Step 3: Create empty test dirs and venv, install deps**

Run:
```bash
cd /Users/yash/Documents/checkout-cascade
mkdir -p payment-service/tests checkout-service/tests loadgen/tests
python3 -m venv .venv && . .venv/bin/activate
pip install -U pip && pip install -r requirements-dev.txt
```
Expected: installs succeed.

- [ ] **Step 4: Verify pytest runs (collects nothing yet)**

Run: `cd /Users/yash/Documents/checkout-cascade && . .venv/bin/activate && pytest`
Expected: `no tests ran` (exit code 5) — confirms config is valid.

- [ ] **Step 5: Commit**

```bash
git add requirements-dev.txt pytest.ini payment-service/requirements.txt payment-service/pytest.ini checkout-service/requirements.txt loadgen/requirements.txt
git commit -m "chore: scaffold services, deps, and pytest config"
```

---

## Task 2: `metrics.py` — rolling-window metrics (TDD)

**Files:**
- Create: `payment-service/metrics.py`
- Test: `payment-service/tests/test_metrics.py`

- [ ] **Step 1: Write the failing tests**

`payment-service/tests/test_metrics.py`:
```python
from metrics import Metrics, now_iso


def test_empty_window_is_guarded():
    m = Metrics()
    s = m.snapshot()
    assert s["requests"] == 0
    assert s["errors"] == 0
    assert s["error_rate"] == 0.0
    assert s["latency_ms"] == {"p50": None, "p95": None}
    assert s["db_query_ms"] == {"p50": None, "p95": None}
    assert s["recent"] == []


def test_counts_errors_and_rate():
    m = Metrics()
    m.record(latency_ms=10, ok=True, status_code=200, db_ms=5)
    m.record(latency_ms=20, ok=False, status_code=500, cause="random_error")
    s = m.snapshot()
    assert s["requests"] == 2
    assert s["errors"] == 1
    assert round(s["error_rate"], 3) == 0.5


def test_percentiles_single_and_many():
    m = Metrics()
    m.record(latency_ms=100, ok=True, status_code=200)
    s = m.snapshot()
    assert s["latency_ms"]["p50"] == 100 and s["latency_ms"]["p95"] == 100
    m2 = Metrics()
    for v in range(1, 101):  # 1..100
        m2.record(latency_ms=v, ok=True, status_code=200)
    s2 = m2.snapshot()
    assert 49 <= s2["latency_ms"]["p50"] <= 52   # nearest-rank, tolerant of rounding
    assert s2["latency_ms"]["p95"] >= 95


def test_include_db_false_omits_db_block():
    m = Metrics()
    m.record(latency_ms=10, ok=True, status_code=200)
    s = m.snapshot(include_db=False)
    assert "db_query_ms" not in s


def test_recent_capped_and_newest_last():
    m = Metrics(recent_n=3)
    for i in range(5):
        m.record(latency_ms=i, ok=True, status_code=200, request_id=str(i))
    recent = m.snapshot()["recent"]
    assert len(recent) == 3
    assert [r["request_id"] for r in recent] == ["2", "3", "4"]
    assert recent[0]["ts"].endswith("Z")


def test_now_iso_format():
    ts = now_iso()
    assert ts.endswith("Z") and "T" in ts and ts.count(":") == 2
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest payment-service/tests/test_metrics.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'metrics'`.

- [ ] **Step 3: Implement `metrics.py`**

`payment-service/metrics.py`:
```python
import time
from collections import deque
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def _percentiles(values):
    if not values:
        return {"p50": None, "p95": None}
    s = sorted(values)
    n = len(s)

    def pick(q):
        idx = int(round(q * (n - 1)))
        return s[min(n - 1, max(0, idx))]

    return {"p50": pick(0.50), "p95": pick(0.95)}


class Metrics:
    def __init__(self, window_sec: int = 60, maxlen: int = 600, recent_n: int = 20):
        self.window_sec = window_sec
        self.recent_n = recent_n
        self._records = deque(maxlen=maxlen)  # each: dict with mono ts + payload

    def record(self, latency_ms, ok, status_code, cause=None, db_ms=None, request_id=None):
        self._records.append({
            "mono": time.monotonic(),
            "ts": now_iso(),
            "request_id": request_id,
            "latency_ms": latency_ms,
            "ok": bool(ok),
            "status_code": status_code,
            "db_ms": db_ms,
            "cause": cause,
        })

    def snapshot(self, include_db: bool = True) -> dict:
        cutoff = time.monotonic() - self.window_sec
        window = [r for r in list(self._records) if r["mono"] >= cutoff]  # snapshot copy
        requests = len(window)
        errors = sum(1 for r in window if not r["ok"])
        error_rate = (errors / requests) if requests else 0.0
        out = {
            "window_sec": self.window_sec,
            "requests": requests,
            "errors": errors,
            "error_rate": round(error_rate, 3),
            "latency_ms": _percentiles([r["latency_ms"] for r in window]),
            "recent": [
                {k: r[k] for k in ("ts", "request_id", "latency_ms", "ok", "status_code", "db_ms", "cause")}
                for r in window[-self.recent_n:]
            ],
        }
        if include_db:
            out["db_query_ms"] = _percentiles([r["db_ms"] for r in window if r["db_ms"] is not None])
        return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest payment-service/tests/test_metrics.py -v`
Expected: PASS. (If `test_percentiles_single_and_many` disagrees on the exact p50 value, adjust the assertion to the nearest-rank result the spec defines — `s[min(n-1, round(0.5*(n-1)))]`; do not change the formula.)

- [ ] **Step 5: Commit**

```bash
git add payment-service/metrics.py payment-service/tests/test_metrics.py
git commit -m "feat(payment): metrics ring buffer with guarded percentiles"
```

---

## Task 3: `chaos.py` (payment) — flags, parse, validate, clamp (TDD)

**Files:**
- Create: `payment-service/chaos.py`
- Test: `payment-service/tests/test_chaos.py`

- [ ] **Step 1: Write the failing tests**

`payment-service/tests/test_chaos.py`:
```python
import pytest
from chaos import Chaos, ChaosError


def make():
    return Chaos(db_slowdown_max=12.0)


def test_defaults():
    c = make()
    assert c.as_dict() == {
        "db_slowdown": 0.0, "random_error_rate": 0.0,
        "memory_leak": False, "payment_failure": False,
    }


def test_set_float_and_clamp():
    c = make()
    assert c.set("db_slowdown", "6") == 6.0
    assert c.set("db_slowdown", "9999") == 12.0           # clamped to max
    assert c.set("random_error_rate", "2.0") == 1.0       # clamped to [0,1]
    assert c.as_dict()["db_slowdown"] == 12.0


def test_set_bool_tokens():
    c = make()
    assert c.set("memory_leak", "TRUE") is True
    assert c.set("payment_failure", "off") is False


def test_unknown_flag_rejected():
    c = make()
    with pytest.raises(ChaosError):
        c.set("own_latency", "1")   # checkout-only flag


def test_bad_value_rejected():
    c = make()
    with pytest.raises(ChaosError):
        c.set("db_slowdown", "abc")
    with pytest.raises(ChaosError):
        c.set("db_slowdown", "-5")
    with pytest.raises(ChaosError):
        c.set("memory_leak", "maybe")


def test_reset():
    c = make()
    c.set("db_slowdown", "6"); c.set("memory_leak", "true")
    c.reset()
    assert c.as_dict() == {"db_slowdown": 0.0, "random_error_rate": 0.0,
                           "memory_leak": False, "payment_failure": False}
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest payment-service/tests/test_chaos.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'chaos'`.

- [ ] **Step 3: Implement `chaos.py`**

`payment-service/chaos.py`:
```python
class ChaosError(Exception):
    pass


_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def _parse_float(value, lo, hi):
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ChaosError(f"value '{value}' is not a number")
    if v < 0:
        raise ChaosError("value must be >= 0")
    return max(lo, min(hi, v))


def _parse_bool(value):
    t = str(value).strip().lower()
    if t in _TRUE:
        return True
    if t in _FALSE:
        return False
    raise ChaosError(f"value '{value}' is not a boolean")


class Chaos:
    def __init__(self, db_slowdown_max: float):
        self.db_slowdown_max = db_slowdown_max
        self.db_slowdown = 0.0
        self.random_error_rate = 0.0
        self.memory_leak = False
        self.payment_failure = False

    def set(self, flag, value):
        if flag == "db_slowdown":
            self.db_slowdown = _parse_float(value, 0.0, self.db_slowdown_max)
            return self.db_slowdown
        if flag == "random_error_rate":
            self.random_error_rate = _parse_float(value, 0.0, 1.0)
            return self.random_error_rate
        if flag == "memory_leak":
            self.memory_leak = _parse_bool(value)
            return self.memory_leak
        if flag == "payment_failure":
            self.payment_failure = _parse_bool(value)
            return self.payment_failure
        raise ChaosError(f"unknown flag '{flag}'")

    def reset(self):
        self.db_slowdown = 0.0
        self.random_error_rate = 0.0
        self.memory_leak = False
        self.payment_failure = False

    def as_dict(self):
        return {
            "db_slowdown": self.db_slowdown,
            "random_error_rate": self.random_error_rate,
            "memory_leak": self.memory_leak,
            "payment_failure": self.payment_failure,
        }
```
- [ ] **Step 4: Run to verify it passes**

Run: `pytest payment-service/tests/test_chaos.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add payment-service/chaos.py payment-service/tests/test_chaos.py
git commit -m "feat(payment): chaos flags with parse/validate/clamp"
```

---

## Task 4: `logging_setup.py` — structured logger (TDD)

**Files:**
- Create: `payment-service/logging_setup.py`
- Test: `payment-service/tests/test_logging.py`

- [ ] **Step 1: Write the failing test**

`payment-service/tests/test_logging.py`:
```python
import re
from logging_setup import make_logger


def test_log_line_format(capsys):
    log = make_logger("payment-service")
    log("charge_received", request_id="ab12", user_id="demo", amount=49.99)
    out = capsys.readouterr().out.strip()
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z INFO  payment-service event=charge_received ", out)
    assert "request_id=ab12" in out and "amount=49.99" in out


def test_log_level(capsys):
    log = make_logger("payment-service")
    log("chaos_set", level="WARN", flag="db_slowdown", value=6.0)
    out = capsys.readouterr().out
    assert "WARN  payment-service event=chaos_set" in out
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest payment-service/tests/test_logging.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'logging_setup'`.

- [ ] **Step 3: Implement `logging_setup.py`**

`payment-service/logging_setup.py`:
```python
import sys
from metrics import now_iso


def make_logger(service: str):
    def log(event: str, level: str = "INFO", **fields):
        parts = [f"{now_iso()} {level:<5} {service} event={event}"]
        for k, v in fields.items():
            parts.append(f"{k}={v}")
        print(" ".join(parts), file=sys.stdout, flush=True)
    return log
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest payment-service/tests/test_logging.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add payment-service/logging_setup.py payment-service/tests/test_logging.py
git commit -m "feat(payment): structured one-line logger"
```

---

## Task 5: `payment-service/db.py` — pools, migration, charge, health

**Files:**
- Create: `payment-service/db.py`
- Test: covered indirectly by integration (Task 5 step 4 is a real-DB smoke test, skippable).

> DB functions are thin wrappers over asyncpg and are best verified against a real Postgres. The endpoint tests (Task 6) stub these functions, so they need no DB. This task provides a manual integration smoke test.

- [ ] **Step 1: Implement `db.py`**

`payment-service/db.py`:
```python
import time
import asyncpg


async def create_pools(dsn, pool_max, health_max, statement_timeout_ms):
    server_settings = {"statement_timeout": str(int(statement_timeout_ms))}
    charge = await asyncpg.create_pool(
        dsn, min_size=2, max_size=int(pool_max), server_settings=server_settings,
    )
    health = await asyncpg.create_pool(
        dsn, min_size=1, max_size=int(health_max),
    )
    return charge, health


async def run_migration(charge_pool):
    await charge_pool.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            amount NUMERIC(10,2) NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )


async def run_charge(charge_pool, user_id, amount, db_slowdown, acquire_timeout):
    started = time.monotonic()
    async with charge_pool.acquire(timeout=acquire_timeout) as conn:
        if db_slowdown and db_slowdown > 0:
            await conn.execute("SELECT pg_sleep($1)", float(db_slowdown))
        tx_id = await conn.fetchval(
            "INSERT INTO transactions (user_id, amount, status) VALUES ($1, $2, 'success') RETURNING id",
            user_id, amount,
        )
        await conn.fetchrow("SELECT id FROM transactions WHERE id = $1", tx_id)  # verifying read (not validated)
    return tx_id, (time.monotonic() - started) * 1000.0


async def health_ping(health_pool, timeout):
    async with health_pool.acquire(timeout=timeout) as conn:
        return (await conn.fetchval("SELECT 1")) == 1
```

- [ ] **Step 2: Start a local Postgres for the smoke test**

Run:
```bash
docker run -d --name cc-pg -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=checkout_cascade -p 5432:5432 postgres:16
sleep 4
```

- [ ] **Step 3: Smoke-test the DB layer**

Run:
```bash
cd /Users/yash/Documents/checkout-cascade && . .venv/bin/activate
DSN="postgresql://postgres:pw@localhost:5432/checkout_cascade" python -c "
import asyncio, db
async def main():
    cp, hp = await db.create_pools('$DSN', 5, 2, 20000)
    await db.run_migration(cp)
    tx, ms = await db.run_charge(cp, 'demo', 1.50, 0, 5)
    print('charge ok', tx, round(ms,1), 'health', await db.health_ping(hp, 0.5))
    await cp.close(); await hp.close()
asyncio.run(main())
"
```
Expected: `charge ok <int> <ms> health True`.

- [ ] **Step 4: Verify `pg_sleep` holds the connection (timing)**

Run:
```bash
DSN="postgresql://postgres:pw@localhost:5432/checkout_cascade" python -c "
import asyncio, time, db
async def main():
    cp, hp = await db.create_pools('$DSN', 5, 2, 20000)
    await db.run_migration(cp)
    t=time.monotonic(); _,ms=await db.run_charge(cp,'demo',1.0,2,5)
    print('elapsed_s', round(time.monotonic()-t,2), 'db_ms', round(ms))
    await cp.close(); await hp.close()
asyncio.run(main())
"
```
Expected: `elapsed_s ~2.x db_ms ~2000` (confirms pg_sleep delay is real).

- [ ] **Step 5: Commit**

```bash
git add payment-service/db.py
git commit -m "feat(payment): asyncpg pools, migration, charge, health"
```

---

## Task 6: `payment-service/main.py` — endpoints (TDD with stubbed DB)

**Files:**
- Create: `payment-service/main.py`
- Test: `payment-service/tests/test_main.py`

- [ ] **Step 1: Write the failing tests**

`payment-service/tests/test_main.py`:
```python
import pytest
from fastapi.testclient import TestClient
import main as app_module

KEY = "test-key"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CHAOS_ADMIN_KEY", KEY)
    monkeypatch.setenv("DB_SLOWDOWN_MAX_SECONDS", "12")
    monkeypatch.delenv("DATABASE_URL", raising=False)  # skip real pools in lifespan

    async def fake_charge(charge_pool, user_id, amount, db_slowdown, acquire_timeout):
        return 1, 5.0
    async def fake_health(health_pool, timeout):
        return True
    monkeypatch.setattr(app_module.db, "run_charge", fake_charge)
    monkeypatch.setattr(app_module.db, "health_ping", fake_health)
    with TestClient(app_module.app) as c:
        yield c


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_charge_success_records_metric(client):
    r = client.post("/charge", json={"user_id": "demo", "amount": 9.99},
                    headers={"X-Request-Id": "ab12"})
    assert r.status_code == 200 and r.json()["status"] == "success"
    s = client.get("/status").json()
    assert s["service"] == "payment-service"
    assert s["requests"] == 1 and s["errors"] == 0


def test_payment_failure_flag(client):
    client.post("/admin/chaos/payment_failure/true", headers={"X-Chaos-Key": KEY})
    r = client.post("/charge", json={"user_id": "d", "amount": 1})
    assert r.status_code == 502
    s = client.get("/status").json()
    assert s["errors"] == 1 and s["error_rate"] == 1.0   # failure WAS recorded


def test_random_error_always(client):
    client.post("/admin/chaos/random_error_rate/1.0", headers={"X-Chaos-Key": KEY})
    r = client.post("/charge", json={"user_id": "d", "amount": 1})
    assert r.status_code == 500


def test_admin_requires_key(client):
    assert client.post("/admin/chaos/db_slowdown/6").status_code == 401


def test_unknown_flag_400(client):
    r = client.post("/admin/chaos/own_latency/1", headers={"X-Chaos-Key": KEY})
    assert r.status_code == 400


def test_db_slowdown_clamped_echo(client):
    r = client.post("/admin/chaos/db_slowdown/9999", headers={"X-Chaos-Key": KEY})
    assert r.status_code == 200 and r.json()["value"] == 12.0


def test_db_error_returns_503(client, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(app_module.db, "run_charge", boom)
    r = client.post("/charge", json={"user_id": "d", "amount": 1})
    assert r.status_code == 503
    assert client.get("/status").json()["errors"] == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest payment-service/tests/test_main.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'main'` (or import error).

- [ ] **Step 3: Implement `main.py`**

`payment-service/main.py`:
```python
import asyncio
import os
import random
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

import db
from chaos import Chaos, ChaosError
from metrics import Metrics
from logging_setup import make_logger

SERVICE = "payment-service"
log = make_logger(SERVICE)

DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", "30"))
HEALTH_POOL_MAX = int(os.environ.get("HEALTH_POOL_MAX", "2"))
DB_SLOWDOWN_MAX = float(os.environ.get("DB_SLOWDOWN_MAX_SECONDS", "12"))
STATEMENT_TIMEOUT_MS = int(os.environ.get("DB_STATEMENT_TIMEOUT_MS", "20000"))
MEMORY_LEAK_CAP_MB = min(int(os.environ.get("MEMORY_LEAK_CAP_MB", "200")), 250)  # hard cap
ACQUIRE_TIMEOUT = float(os.environ.get("DB_ACQUIRE_TIMEOUT", "5"))
HEALTH_DB_TIMEOUT = float(os.environ.get("HEALTH_DB_TIMEOUT", "0.5"))
ADMIN_KEY = os.environ.get("CHAOS_ADMIN_KEY", "")

chaos = Chaos(db_slowdown_max=DB_SLOWDOWN_MAX)
metrics = Metrics()
_leak = []  # list of bytearrays


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.charge_pool = None
    app.state.health_pool = None
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        for attempt in range(1, 8):
            try:
                cp, hp = await db.create_pools(dsn, DB_POOL_MAX, HEALTH_POOL_MAX, STATEMENT_TIMEOUT_MS)
                await db.run_migration(cp)
                app.state.charge_pool, app.state.health_pool = cp, hp
                log("startup_db_ready", attempt=attempt)
                break
            except Exception as e:  # noqa: BLE001
                log("startup_db_retry", level="WARN", attempt=attempt, error=type(e).__name__)
                if attempt == 7:
                    log("startup_db_failed", level="ERROR")
                    raise
                await asyncio.sleep(min(2 ** attempt, 10))
    else:
        log("startup_no_db", level="WARN")  # test/local mode
    yield
    for p in (app.state.charge_pool, app.state.health_pool):
        if p:
            await p.close()


app = FastAPI(lifespan=lifespan)


def _require_key(x_chaos_key):
    if x_chaos_key != ADMIN_KEY or not ADMIN_KEY:
        raise HTTPException(status_code=401, detail="bad chaos key")


@app.get("/health")
async def health(request: Request):
    pool = request.app.state.health_pool
    if pool is None:
        return {"status": "ok"}  # local/test mode
    try:
        ok = await db.health_ping(pool, HEALTH_DB_TIMEOUT)
        return {"status": "ok"} if ok else JSONResponse(status_code=503, content={"status": "db_unreachable"})
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"status": "db_unreachable"})


@app.get("/status")
async def status():
    s = {"service": SERVICE, **metrics.snapshot(include_db=True),
         "chaos": chaos.as_dict(), "memory_leak_mb": len(_leak)}
    return s


@app.post("/admin/chaos/reset")
async def chaos_reset(x_chaos_key: str = Header(default="")):
    _require_key(x_chaos_key)
    chaos.reset()
    _leak.clear()
    log("chaos_set", level="WARN", flag="reset", value="all")
    return {"status": "reset", "chaos": chaos.as_dict()}


@app.post("/admin/chaos/{flag}/{value}")
async def chaos_set(flag: str, value: str, x_chaos_key: str = Header(default="")):
    _require_key(x_chaos_key)
    try:
        effective = chaos.set(flag, value)
    except ChaosError as e:
        raise HTTPException(status_code=400, detail=str(e))
    log("chaos_set", level="WARN", flag=flag, value=effective)
    return {"flag": flag, "value": effective}


@app.post("/charge")
async def charge(request: Request):
    body = await request.json()
    user_id = body.get("user_id", "unknown")
    amount = body.get("amount", 0)
    rid = request.headers.get("X-Request-Id")
    started = time.monotonic()
    status_code, ok, cause, db_ms = 200, True, None, None
    log("charge_received", request_id=rid, user_id=user_id, amount=amount)
    try:
        if chaos.payment_failure:
            status_code, ok, cause = 502, False, "payment_failure"
            log("charge_failed", level="ERROR", request_id=rid, cause=cause)
            return JSONResponse(status_code=502, content={"error": "Payment gateway timeout"})
        if chaos.random_error_rate > 0 and random.random() < chaos.random_error_rate:
            status_code, ok, cause = 500, False, "random_error"
            log("charge_failed", level="ERROR", request_id=rid, cause=cause)
            return JSONResponse(status_code=500, content={"error": "internal error"})
        if chaos.memory_leak and len(_leak) < MEMORY_LEAK_CAP_MB:
            _leak.append(bytearray(1024 * 1024))
        log("db_query_start", request_id=rid)
        tx_id, db_ms = await db.run_charge(
            request.app.state.charge_pool, user_id, amount, chaos.db_slowdown, ACQUIRE_TIMEOUT)
        log("db_query_done", request_id=rid, duration_ms=round(db_ms))
        log("charge_success", request_id=rid)
        return {"status": "success", "transaction_id": tx_id}
    except Exception as e:  # noqa: BLE001
        status_code, ok, cause = 503, False, "db_error"
        log("charge_failed", level="ERROR", request_id=rid, cause=cause, error=type(e).__name__)
        return JSONResponse(status_code=503, content={"error": "database error"})
    finally:
        metrics.record(latency_ms=(time.monotonic() - started) * 1000.0, ok=ok,
                       status_code=status_code, cause=cause, db_ms=db_ms, request_id=rid)
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest payment-service/tests/test_main.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Run the full payment suite**

Run: `pytest payment-service/tests -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add payment-service/main.py payment-service/tests/test_main.py
git commit -m "feat(payment): FastAPI endpoints (charge/health/status/admin) with chaos + metrics"
```

---

## Task 7: checkout-service shared helpers (copy identical files)

**Files:**
- Create: `checkout-service/metrics.py` (identical to `payment-service/metrics.py`)
- Create: `checkout-service/logging_setup.py` (identical to `payment-service/logging_setup.py`)
- Create: `checkout-service/pytest.ini`

- [ ] **Step 1: Copy the two identical helpers and add pytest config**

Run:
```bash
cd /Users/yash/Documents/checkout-cascade
cp payment-service/metrics.py checkout-service/metrics.py
cp payment-service/logging_setup.py checkout-service/logging_setup.py
cp payment-service/pytest.ini checkout-service/pytest.ini
```
(These files are byte-identical by design — the spec mandates self-contained services with no cross-folder imports. Their content is exactly as defined in Tasks 2 and 4.)

- [ ] **Step 2: Verify they import**

Run: `cd /Users/yash/Documents/checkout-cascade && . .venv/bin/activate && python -c "import sys; sys.path.insert(0,'checkout-service'); import metrics, logging_setup; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add checkout-service/metrics.py checkout-service/logging_setup.py checkout-service/pytest.ini
git commit -m "feat(checkout): copy shared metrics + logging helpers"
```

---

## Task 8: `checkout-service/chaos.py` — flags (TDD)

**Files:**
- Create: `checkout-service/chaos.py`
- Test: `checkout-service/tests/test_chaos.py`

- [ ] **Step 1: Write the failing tests**

`checkout-service/tests/test_chaos.py`:
```python
import pytest
from chaos import Chaos, ChaosError


def make():
    return Chaos(own_latency_max=10.0)


def test_defaults():
    assert make().as_dict() == {"own_latency": 0.0, "random_error_rate": 0.0}


def test_set_and_clamp():
    c = make()
    assert c.set("own_latency", "2.5") == 2.5
    assert c.set("own_latency", "999") == 10.0
    assert c.set("random_error_rate", "2") == 1.0


def test_payment_only_flag_rejected():
    with pytest.raises(ChaosError):
        make().set("db_slowdown", "6")


def test_bad_value():
    with pytest.raises(ChaosError):
        make().set("own_latency", "abc")


def test_reset():
    c = make(); c.set("own_latency", "3")
    c.reset()
    assert c.as_dict() == {"own_latency": 0.0, "random_error_rate": 0.0}
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest checkout-service/tests/test_chaos.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `checkout-service/chaos.py`**

```python
class ChaosError(Exception):
    pass


def _parse_float(value, lo, hi):
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ChaosError(f"value '{value}' is not a number")
    if v < 0:
        raise ChaosError("value must be >= 0")
    return max(lo, min(hi, v))


class Chaos:
    def __init__(self, own_latency_max: float):
        self.own_latency_max = own_latency_max
        self.own_latency = 0.0
        self.random_error_rate = 0.0

    def set(self, flag, value):
        if flag == "own_latency":
            self.own_latency = _parse_float(value, 0.0, self.own_latency_max)
            return self.own_latency
        if flag == "random_error_rate":
            self.random_error_rate = _parse_float(value, 0.0, 1.0)
            return self.random_error_rate
        raise ChaosError(f"unknown flag '{flag}'")

    def reset(self):
        self.own_latency = 0.0
        self.random_error_rate = 0.0

    def as_dict(self):
        return {"own_latency": self.own_latency, "random_error_rate": self.random_error_rate}
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest checkout-service/tests/test_chaos.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add checkout-service/chaos.py checkout-service/tests/test_chaos.py
git commit -m "feat(checkout): chaos flags (own_latency, random_error_rate)"
```

---

## Task 9: `checkout-service/main.py` — endpoints (TDD with mocked httpx)

**Files:**
- Create: `checkout-service/main.py`
- Test: `checkout-service/tests/test_main.py`

- [ ] **Step 1: Write the failing tests**

`checkout-service/tests/test_main.py`:
```python
import httpx
import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
import main as app_module

KEY = "test-key"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CHAOS_ADMIN_KEY", KEY)
    monkeypatch.setenv("PAYMENT_SERVICE_URL", "payment:10000")
    with TestClient(app_module.app) as c:
        yield c


def _resp(status, payload):
    return httpx.Response(status_code=status, json=payload, request=httpx.Request("POST", "http://x"))


def test_health_ok(client):
    assert client.get("/health").json()["status"] == "ok"


def test_checkout_success(client):
    client.app.state.client.post = AsyncMock(return_value=_resp(200, {"status": "success"}))
    r = client.post("/checkout", json={"user_id": "demo", "amount": 9.99})
    assert r.status_code == 200
    s = client.get("/status").json()
    assert s["service"] == "checkout-service" and s["requests"] == 1 and s["errors"] == 0
    assert "db_query_ms" not in s and "memory_leak_mb" not in s
    assert set(s["chaos"].keys()) == {"own_latency", "random_error_rate"}


def test_checkout_timeout_504(client):
    client.app.state.client.post = AsyncMock(side_effect=httpx.ReadTimeout("t"))
    r = client.post("/checkout", json={"user_id": "d", "amount": 1})
    assert r.status_code == 504
    assert client.get("/status").json()["errors"] == 1


def test_checkout_payment_5xx_502(client):
    client.app.state.client.post = AsyncMock(return_value=_resp(503, {"error": "x"}))
    r = client.post("/checkout", json={"user_id": "d", "amount": 1})
    assert r.status_code == 502


def test_checkout_connect_error_502(client):
    client.app.state.client.post = AsyncMock(side_effect=httpx.ConnectError("c"))
    r = client.post("/checkout", json={"user_id": "d", "amount": 1})
    assert r.status_code == 502


def test_own_random_error(client):
    client.post("/admin/chaos/random_error_rate/1.0", headers={"X-Chaos-Key": KEY})
    r = client.post("/checkout", json={"user_id": "d", "amount": 1})
    assert r.status_code == 500


def test_admin_requires_key(client):
    assert client.post("/admin/chaos/own_latency/1").status_code == 401
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest checkout-service/tests/test_main.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `checkout-service/main.py`**

```python
import os
import random
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from chaos import Chaos, ChaosError
from metrics import Metrics
from logging_setup import make_logger

SERVICE = "checkout-service"
log = make_logger(SERVICE)

PAYMENT_URL = os.environ.get("PAYMENT_SERVICE_URL", "")
PAYMENT_TIMEOUT = float(os.environ.get("PAYMENT_TIMEOUT_SECONDS", "3"))
OWN_LATENCY_MAX = float(os.environ.get("OWN_LATENCY_MAX_SECONDS", "10"))
ADMIN_KEY = os.environ.get("CHAOS_ADMIN_KEY", "")

chaos = Chaos(own_latency_max=OWN_LATENCY_MAX)
metrics = Metrics()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = httpx.AsyncClient(timeout=PAYMENT_TIMEOUT)
    yield
    await app.state.client.aclose()


app = FastAPI(lifespan=lifespan)


def _require_key(x_chaos_key):
    if x_chaos_key != ADMIN_KEY or not ADMIN_KEY:
        raise HTTPException(status_code=401, detail="bad chaos key")


def _base_url():
    return f"http://{PAYMENT_URL}"


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/status")
async def status():
    return {"service": SERVICE, **metrics.snapshot(include_db=False), "chaos": chaos.as_dict()}


@app.post("/admin/chaos/reset")
async def chaos_reset(x_chaos_key: str = Header(default="")):
    _require_key(x_chaos_key)
    chaos.reset()
    log("chaos_set", level="WARN", flag="reset", value="all")
    return {"status": "reset", "chaos": chaos.as_dict()}


@app.post("/admin/chaos/{flag}/{value}")
async def chaos_set(flag: str, value: str, x_chaos_key: str = Header(default="")):
    _require_key(x_chaos_key)
    try:
        effective = chaos.set(flag, value)
    except ChaosError as e:
        raise HTTPException(status_code=400, detail=str(e))
    log("chaos_set", level="WARN", flag=flag, value=effective)
    return {"flag": flag, "value": effective}


@app.post("/checkout")
async def checkout(request: Request):
    body = await request.json()
    user_id = body.get("user_id", "unknown")
    amount = body.get("amount", 0)
    rid = uuid.uuid4().hex[:8]
    started = time.monotonic()
    status_code, ok, cause = 200, True, None
    log("checkout_received", request_id=rid, user_id=user_id, amount=amount)
    try:
        if chaos.random_error_rate > 0 and random.random() < chaos.random_error_rate:
            status_code, ok, cause = 500, False, "random_error"
            log("checkout_failed", level="ERROR", request_id=rid, cause=cause)
            return JSONResponse(status_code=500, content={"error": "internal error"})
        if chaos.own_latency > 0:
            import asyncio
            await asyncio.sleep(chaos.own_latency)
        log("payment_call_start", request_id=rid)
        call_started = time.monotonic()
        try:
            resp = await request.app.state.client.post(
                f"{_base_url()}/charge", json={"user_id": user_id, "amount": amount},
                headers={"X-Request-Id": rid}, timeout=PAYMENT_TIMEOUT)
        except httpx.TimeoutException:
            status_code, ok, cause = 504, False, "payment_timeout"
            log("payment_call_result", level="WARN", request_id=rid, status="timeout",
                elapsed_ms=round((time.monotonic() - call_started) * 1000))
            return JSONResponse(status_code=504, content={"error": "timeout calling payment-service"})
        except httpx.HTTPError:
            status_code, ok, cause = 502, False, "conn_error"
            log("payment_call_result", level="ERROR", request_id=rid, status="error", code="conn_error")
            return JSONResponse(status_code=502, content={"error": "payment-service unreachable"})
        if resp.status_code >= 500:
            status_code, ok, cause = 502, False, f"payment_{resp.status_code}"
            log("payment_call_result", level="ERROR", request_id=rid, status="error", code=resp.status_code)
            return JSONResponse(status_code=502, content={"error": "payment-service error"})
        log("payment_call_result", request_id=rid, status="ok")
        log("checkout_success", request_id=rid)
        return {"status": "success"}
    finally:
        metrics.record(latency_ms=(time.monotonic() - started) * 1000.0, ok=ok,
                       status_code=status_code, cause=cause, request_id=rid)
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest checkout-service/tests -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add checkout-service/main.py checkout-service/tests/test_main.py
git commit -m "feat(checkout): FastAPI endpoints (checkout/health/status/admin) with request_id + httpx handling"
```

---

## Task 10: `loadgen/run.py` — open-loop scheduler (TDD on the scheduler logic)

**Files:**
- Create: `loadgen/run.py`
- Test: `loadgen/tests/test_scheduler.py`

> The scheduler's testable core is "fire on schedule, cap in-flight, count outcomes" — extracted into a `LoadGen` class so it can be tested without real HTTP or wall-clock sleeps.

- [ ] **Step 1: Write the failing tests**

`loadgen/tests/test_scheduler.py`:
```python
import asyncio
import pytest
from run import LoadGen


@pytest.mark.asyncio
async def test_respects_max_inflight():
    sent = {"n": 0}
    release = asyncio.Event()

    async def slow_send():
        sent["n"] += 1
        await release.wait()

    lg = LoadGen(send=slow_send, rps=1000, max_inflight=3)
    # fire 10 ticks quickly; only 3 should be in flight, rest skipped
    skipped = 0
    for _ in range(10):
        if not lg.try_fire():
            skipped += 1
    await asyncio.sleep(0.01)
    assert lg.inflight == 3
    assert skipped == 7
    release.set()
    await asyncio.sleep(0.01)
    assert lg.inflight == 0
    assert lg.stats["ok"] == 3


@pytest.mark.asyncio
async def test_counts_failures():
    async def boom():
        raise RuntimeError("x")
    lg = LoadGen(send=boom, rps=10, max_inflight=5)
    lg.try_fire()
    await asyncio.sleep(0.01)
    assert lg.stats["failed"] == 1 and lg.inflight == 0
```

Add `loadgen/tests/` needs async support. Add to `requirements-dev.txt`: `pytest-asyncio==0.24.*`, and create `loadgen/pytest.ini`:
```ini
[pytest]
addopts = -q
pythonpath = .
asyncio_mode = auto
```

- [ ] **Step 2: Run to verify it fails**

Run: `pip install pytest-asyncio==0.24.* && pytest loadgen/tests -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `loadgen/run.py`**

```python
import asyncio
import os
import random


class LoadGen:
    def __init__(self, send, rps, max_inflight):
        self.send = send
        self.rps = rps
        self.max_inflight = max_inflight
        self.inflight = 0
        self.stats = {"ok": 0, "failed": 0, "skipped": 0}

    def try_fire(self) -> bool:
        if self.inflight >= self.max_inflight:
            self.stats["skipped"] += 1
            return False
        self.inflight += 1
        asyncio.ensure_future(self._run())
        return True

    async def _run(self):
        try:
            await self.send()
            self.stats["ok"] += 1
        except Exception:  # noqa: BLE001
            self.stats["failed"] += 1
        finally:
            self.inflight -= 1


async def _main():
    import httpx
    base = f"http://{os.environ['CHECKOUT_SERVICE_URL']}"
    rps = float(os.environ.get("LOADGEN_RPS", "2"))
    timeout = float(os.environ.get("LOADGEN_TIMEOUT_SECONDS", "20"))
    max_inflight = int(os.environ.get("LOADGEN_MAX_INFLIGHT", "50"))
    client = httpx.AsyncClient(timeout=timeout)

    async def send():
        await client.post(f"{base}/checkout",
                          json={"user_id": f"u{random.randint(1, 9999)}",
                                "amount": round(random.uniform(5, 200), 2)})

    lg = LoadGen(send=send, rps=rps, max_inflight=max_inflight)
    interval = 1.0 / rps
    tick = 0
    while True:
        lg.try_fire()
        tick += 1
        if tick % max(1, int(rps * 10)) == 0:  # ~every 10s
            print(f"loadgen sent_ok={lg.stats['ok']} failed={lg.stats['failed']} "
                  f"skipped={lg.stats['skipped']} inflight={lg.inflight}", flush=True)
        await asyncio.sleep(interval * random.uniform(0.7, 1.3))  # jitter


if __name__ == "__main__":
    asyncio.run(_main())
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest loadgen/tests -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add loadgen/run.py loadgen/tests/test_scheduler.py loadgen/pytest.ini requirements-dev.txt
git commit -m "feat(loadgen): open-loop scheduler with in-flight cap"
```

---

## Task 11: `render.yaml` Blueprint

**Files:**
- Create: `render.yaml`

- [ ] **Step 1: Create the Blueprint (copied verbatim from spec §6, verified in §14)**

`render.yaml`:
```yaml
databases:
  - name: checkout-cascade-db
    plan: basic-256mb
    databaseName: checkout_cascade
    user: checkout_cascade

envVarGroups:
  - name: chaos-shared
    envVars:
      - key: CHAOS_ADMIN_KEY
        generateValue: true

services:
  - type: web
    name: payment-service
    runtime: python
    plan: starter
    numInstances: 1
    rootDir: payment-service
    buildCommand: pip install -r requirements.txt
    startCommand: uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1
    healthCheckPath: /health
    envVars:
      - fromGroup: chaos-shared
      - key: DATABASE_URL
        fromDatabase:
          name: checkout-cascade-db
          property: connectionString
      - { key: DB_POOL_MAX, value: "30" }
      - { key: HEALTH_POOL_MAX, value: "2" }
      - { key: DB_SLOWDOWN_MAX_SECONDS, value: "12" }
      - { key: DB_STATEMENT_TIMEOUT_MS, value: "20000" }
      - { key: MEMORY_LEAK_CAP_MB, value: "200" }

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
          property: hostport
      - { key: PAYMENT_TIMEOUT_SECONDS, value: "3" }
      - { key: OWN_LATENCY_MAX_SECONDS, value: "10" }

  - type: worker
    name: loadgen
    runtime: python
    plan: starter
    rootDir: loadgen
    buildCommand: pip install -r requirements.txt
    startCommand: python run.py
    envVars:
      - key: CHECKOUT_SERVICE_URL
        fromService:
          name: checkout-service
          type: web
          property: hostport
      - { key: LOADGEN_RPS, value: "2" }
      - { key: LOADGEN_TIMEOUT_SECONDS, value: "20" }
      - { key: LOADGEN_MAX_INFLIGHT, value: "50" }
```

- [ ] **Step 2: Validate YAML parses**

Run: `cd /Users/yash/Documents/checkout-cascade && python -c "import yaml; yaml.safe_load(open('render.yaml')); print('yaml ok')"`
Expected: `yaml ok`. (If PyYAML missing: `pip install pyyaml` first.)

- [ ] **Step 3: Commit**

```bash
git add render.yaml
git commit -m "feat: Render Blueprint (2 web + worker + postgres, internal URLs)"
```

---

## Task 12: README + final full-suite check + deploy validation

**Files:**
- Create: `README.md`

- [ ] **Step 1: Run the entire test suite**

Run: `cd /Users/yash/Documents/checkout-cascade && . .venv/bin/activate && pytest`
Expected: all tests PASS across payment-service, checkout-service, loadgen.

- [ ] **Step 2: Write `README.md`**

`README.md`:
```markdown
# Checkout Cascade (Phase 1)

Two faulty services + a load generator on Render that produce realistic incident data:
`checkout-service` → `payment-service` → Postgres. Checkout looks broken, but the real
root cause is a slow DB call inside payment-service.

## Deploy
1. Push this repo to GitHub.
2. Render → New → Blueprint → pick this repo (`render.yaml`). Approve the 4 resources.
3. After deploy, grab `CHAOS_ADMIN_KEY` from the `chaos-shared` env group, and the public
   URLs of payment-service and checkout-service.

## Endpoints (both web services)
- `POST /checkout` (checkout) / `POST /charge` (payment) — `{user_id, amount}`
- `GET /health` — liveness (chaos-exempt)
- `GET /status` — rich evidence JSON (rolling 60s window)
- `POST /admin/chaos/{flag}/{value}` — header `X-Chaos-Key: <key>`
- `POST /admin/chaos/reset` — header `X-Chaos-Key: <key>`

## Chaos flags
payment: `db_slowdown` (sec, ≤12), `random_error_rate` (0–1), `memory_leak` (bool), `payment_failure` (bool)
checkout: `own_latency` (sec, ≤10), `random_error_rate` (0–1)

## Demo
See `docs/superpowers/specs/2026-06-27-checkout-cascade-design.md` §12 for the full script.

\`\`\`bash
KEY=...; PAY=https://payment-...onrender.com; CHK=https://checkout-...onrender.com
curl -X POST $PAY/admin/chaos/db_slowdown/6 -H "X-Chaos-Key: $KEY"   # hero incident
curl $CHK/status   # checkout: timeouts; payment $PAY/status: db_query_ms p95 ~6000
curl -X POST $PAY/admin/chaos/reset -H "X-Chaos-Key: $KEY"
\`\`\`

## Local test
\`\`\`bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
pytest
\`\`\`
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: README with deploy + demo instructions"
```

- [ ] **Step 4: Deploy & run the spec §13 Definition-of-Done checklist (manual, on Render)**

This is a manual validation gate — not automated. After `git push` and a Blueprint deploy, work through **every** checkbox in spec §13, especially:
- `/status` returns 200 with zero traffic (empty-window guards).
- `db_slowdown/6`: checkout shows timeouts, payment `/status` `db_query_ms.p95≈6000`, and **payment-service is NOT restarted** — verify `/health` stays fast at the demo value AND at `db_slowdown/12`.
- request volume stays steady during the incident (open-loop loadgen).
- `memory_leak/true` trends memory up and caps without crashing.
- per-service flag validation (400s), clamps, and `X-Chaos-Key` 401.

Record results in the PR/commit message. Do not consider phase 1 done until §13 passes on Render.

---

## Notes for the executor
- **Run each test before implementing** (TDD): confirm the RED failure, then write code to GREEN.
- **Commit after every green step.**
- The `payment-service` lifespan **skips DB pool creation when `DATABASE_URL` is unset**, so the test suite needs no Postgres. The Task 5 DB smoke tests are the only steps requiring a local Postgres (Docker).
- `metrics.py` and `logging_setup.py` are intentionally duplicated across services (self-contained deploys); keep them byte-identical — if you change one, copy it to the other.
- Single instance + `--workers 1` is a hard requirement (in-memory state). Do not add workers/instances.
