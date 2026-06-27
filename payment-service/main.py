"""payment-service: the only DB holder. Injects chaos into /charge; /health is chaos-exempt."""
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

chaos = Chaos(db_slowdown_max=DB_SLOWDOWN_MAX)
metrics = Metrics()
_leak = []  # list of 1 MB bytearrays


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
        log("startup_no_db", level="WARN")  # local/test mode
    yield
    for p in (app.state.charge_pool, app.state.health_pool):
        if p:
            await p.close()


app = FastAPI(lifespan=lifespan)


def _require_key(x_chaos_key):
    key = os.environ.get("CHAOS_ADMIN_KEY", "")
    if not key or x_chaos_key != key:
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
    return {"service": SERVICE, **metrics.snapshot(include_db=True),
            "chaos": chaos.as_dict(), "memory_leak_mb": len(_leak)}


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
