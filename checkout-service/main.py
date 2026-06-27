"""checkout-service: calls payment-service over HTTP. No DB. Logs are misleading by design
(it only sees 'timeout calling payment-service', never the DB root cause)."""
import asyncio
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

chaos = Chaos(own_latency_max=OWN_LATENCY_MAX)
metrics = Metrics()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = httpx.AsyncClient(timeout=PAYMENT_TIMEOUT)
    yield
    await app.state.client.aclose()


app = FastAPI(lifespan=lifespan)


def _require_key(x_chaos_key):
    key = os.environ.get("CHAOS_ADMIN_KEY", "")
    if not key or x_chaos_key != key:
        raise HTTPException(status_code=401, detail="bad chaos key")


def _base_url():
    # Accept either an internal host:port (prepend http://) or a full URL (e.g. public https://...onrender.com)
    if PAYMENT_URL.startswith("http://") or PAYMENT_URL.startswith("https://"):
        return PAYMENT_URL.rstrip("/")
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
            await asyncio.sleep(chaos.own_latency)
        log("payment_call_start", request_id=rid)
        call_started = time.monotonic()
        try:
            resp = await request.app.state.client.post(
                f"{_base_url()}/charge",
                json={"user_id": user_id, "amount": amount},
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
