"""Open-loop load generator: fires POST /checkout at a steady rate with jitter, capping
in-flight requests so a slow incident doesn't throttle new traffic (keeps volume visible)."""
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
        await client.post(
            f"{base}/checkout",
            json={"user_id": f"u{random.randint(1, 9999)}",
                  "amount": round(random.uniform(5, 200), 2)})

    lg = LoadGen(send=send, rps=rps, max_inflight=max_inflight)
    interval = 1.0 / rps
    tick = 0
    print(f"loadgen starting target={base} rps={rps}", flush=True)
    while True:
        lg.try_fire()
        tick += 1
        if tick % max(1, int(rps * 10)) == 0:  # ~every 10s
            print(f"loadgen sent_ok={lg.stats['ok']} failed={lg.stats['failed']} "
                  f"skipped={lg.stats['skipped']} inflight={lg.inflight}", flush=True)
        await asyncio.sleep(interval * random.uniform(0.7, 1.3))  # jitter


if __name__ == "__main__":
    asyncio.run(_main())
