"""In-memory rolling-window metrics for /status. Single-process (uvicorn --workers 1).
Identical to payment-service/metrics.py (services are self-contained, no cross-folder imports)."""
import time
from collections import deque
from datetime import datetime, timezone


def now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


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
        self._records = deque(maxlen=maxlen)

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
