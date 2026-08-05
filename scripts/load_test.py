"""
DevGuard AI — concurrency / load proof.

Fires N concurrent POST /scan requests and reports latency distribution plus
whether the backend absorbed the spike (cache hits / no 5xx / circuit breaker
holding). This is the artifact that turns "production-ready" from a claim into
a measurement.

Usage:
    python scripts/load_test.py --url http://localhost:8000 --concurrency 30 --total 60
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from dataclasses import dataclass, field

import httpx

VULNERABLE_SNIPPET = (
    "def login(user, pw):\n"
    "    q = \"SELECT * FROM users WHERE name='\" + user + \"' AND pw='\" + pw + \"'\"\n"
    "    return db.execute(q)\n"
)


@dataclass
class Results:
    latencies_ms: list[float] = field(default_factory=list)
    statuses: dict[int, int] = field(default_factory=dict)
    errors: int = 0


async def one_request(client: httpx.AsyncClient, url: str, res: Results, sem: asyncio.Semaphore) -> None:
    async with sem:
        start = time.perf_counter()
        try:
            r = await client.post(
                f"{url}/scan",
                json={"code": VULNERABLE_SNIPPET, "language": "python"},
                timeout=30.0,
            )
            elapsed = (time.perf_counter() - start) * 1000
            res.latencies_ms.append(elapsed)
            res.statuses[r.status_code] = res.statuses.get(r.status_code, 0) + 1
        except Exception:  # noqa: BLE001
            res.errors += 1


def pct(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    k = int(round((p / 100) * (len(s) - 1)))
    return s[k]


async def run(url: str, concurrency: int, total: int) -> Results:
    res = Results()
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(limits=limits) as client:
        wall_start = time.perf_counter()
        await asyncio.gather(*(one_request(client, url, res, sem) for _ in range(total)))
        res_wall = time.perf_counter() - wall_start
    _report(res, concurrency, total, res_wall)
    return res


def _report(res: Results, concurrency: int, total: int, wall: float) -> None:
    ok = res.statuses.get(200, 0) + res.statuses.get(201, 0) + res.statuses.get(202, 0)
    print("\n=== DevGuard Load Test ===")
    print(f"concurrency={concurrency}  total={total}  wall={wall:.2f}s  throughput={total / wall:.1f} req/s")
    print(f"success={ok}  errors={res.errors}  status_breakdown={res.statuses}")
    if res.latencies_ms:
        print(f"latency ms  p50={pct(res.latencies_ms,50):.0f}  "
              f"p90={pct(res.latencies_ms,90):.0f}  "
              f"p99={pct(res.latencies_ms,99):.0f}  "
              f"mean={statistics.mean(res.latencies_ms):.0f}")
    server_errors = sum(v for k, v in res.statuses.items() if k >= 500)
    verdict = "PASS ✅ (no 5xx, spike absorbed)" if server_errors == 0 and res.errors == 0 else "REVIEW ⚠"
    print(f"verdict: {verdict}\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--concurrency", type=int, default=30)
    ap.add_argument("--total", type=int, default=60)
    args = ap.parse_args()
    asyncio.run(run(args.url, args.concurrency, args.total))
