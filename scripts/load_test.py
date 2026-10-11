"""Small load test for the API and the orchestrator (no extra tools needed beyond httpx).

What it measures
  1. READ PATH   - N concurrent clients hammer GET /documents/search for a fixed time.
                   Shows request throughput, latency percentiles and (via /metrics) the
                   retrieval-cache hit ratio.
  2. WORKFLOWS   - create M runs at once and wait until all are final. Shows how fast the
                   API accepts work (create latency), how long runs take end to end, and
                   how many runs per minute the worker pool finishes.

Run it against a server you started yourself:
    RATE_LIMIT_ENABLED=false uvicorn app.main:create_app --factory
    python scripts/load_test.py --base-url http://localhost:8000 --json results.json
WHY the rate limiter must be off: the default 300 requests/min per IP would (correctly) reject
a load test with HTTP 429, and the numbers would measure the limiter instead of the app.
WHY the mock LLM: results are repeatable and free. Set MOCK_LLM_LATENCY_SECONDS on the SERVER to
simulate model latency; with the instant mock the numbers show orchestration overhead only.
Exit code 1 if any request failed, so it can gate a CI job.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field

import httpx

QUERIES = (
    "refund policy digital goods",
    "incident severity escalation",
    "multi factor authentication",
    "production outage runbook",
    "security policy passwords",
)
GOALS = (
    "What is the refund window for digital goods?",
    "Calculate 1200 * 0.18 for the refund fee",
    "What does the incident runbook say about severity 1 outages?",
)
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}


@dataclass
class Phase:
    name: str
    requests: int = 0
    errors: int = 0
    seconds: float = 0.0
    latencies_ms: list[float] = field(default_factory=list)

    def summary(self) -> dict[str, object]:
        ordered = sorted(self.latencies_ms)
        return {
            "name": self.name,
            "requests": self.requests,
            "errors": self.errors,
            "seconds": round(self.seconds, 2),
            "requests_per_second": round(self.requests / self.seconds, 1) if self.seconds else 0,
            "latency_ms": {
                "p50": percentile(ordered, 50),
                "p95": percentile(ordered, 95),
                "p99": percentile(ordered, 99),
                "max": round(ordered[-1], 1) if ordered else 0,
            },
        }


def percentile(ordered: list[float], pct: float) -> float:
    """Nearest-rank percentile of an already sorted list (0 for an empty list)."""
    if not ordered:
        return 0.0
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return round(ordered[rank - 1], 1)


async def login(client: httpx.AsyncClient, email: str, password: str) -> dict[str, str]:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def metric_total(client: httpx.AsyncClient, name: str, **labels: str) -> float:
    """Sum of a metric's samples matching ``labels`` from /metrics (0 if unavailable)."""
    response = await client.get("/metrics")
    if response.status_code != 200:
        return 0.0
    total = 0.0
    for line in response.text.splitlines():
        if not line.startswith(name + "{") and not line.startswith(name + " "):
            continue
        if all(f'{k}="{v}"' in line for k, v in labels.items()):
            total += float(line.rsplit(" ", 1)[1])
    return total


async def read_path(
    client: httpx.AsyncClient, headers: dict[str, str], concurrency: int, duration: float
) -> Phase:
    phase = Phase("read path: GET /documents/search")
    deadline = time.perf_counter() + duration

    async def worker(index: int) -> None:
        n = index
        while time.perf_counter() < deadline:
            started = time.perf_counter()
            try:
                response = await client.get(
                    "/api/v1/documents/search",
                    params={"q": QUERIES[n % len(QUERIES)]},
                    headers=headers,
                )
                ok = response.status_code == 200
            except httpx.HTTPError:
                ok = False
            phase.latencies_ms.append((time.perf_counter() - started) * 1000)
            phase.requests += 1
            phase.errors += 0 if ok else 1
            n += 1

    started = time.perf_counter()
    await asyncio.gather(*(worker(i) for i in range(concurrency)))
    phase.seconds = time.perf_counter() - started
    return phase


async def workflows(
    client: httpx.AsyncClient, headers: dict[str, str], runs: int, timeout: float
) -> tuple[Phase, Phase, dict[str, int]]:
    create = Phase("workflows: POST /workflows (accept)")
    finish = Phase("workflows: end to end (create -> final status)")
    outcomes: dict[str, int] = {}

    async def one(index: int) -> None:
        started = time.perf_counter()
        try:
            response = await client.post(
                "/api/v1/workflows", json={"goal": GOALS[index % len(GOALS)]}, headers=headers
            )
            response.raise_for_status()
        except httpx.HTTPError:
            create.errors += 1
            create.requests += 1
            return
        create.requests += 1
        create.latencies_ms.append((time.perf_counter() - started) * 1000)
        run_id = response.json()["id"]
        status = "TIMEOUT"
        while time.perf_counter() - started < timeout:
            polled = await client.get(f"/api/v1/workflows/{run_id}", headers=headers)
            if polled.status_code == 200 and polled.json()["status"] in TERMINAL:
                status = polled.json()["status"]
                break
            await asyncio.sleep(0.1)
        finish.requests += 1
        outcomes[status] = outcomes.get(status, 0) + 1
        if status == "COMPLETED":
            finish.latencies_ms.append((time.perf_counter() - started) * 1000)
        else:
            finish.errors += 1

    started = time.perf_counter()
    await asyncio.gather(*(one(i) for i in range(runs)))
    create.seconds = finish.seconds = time.perf_counter() - started
    return create, finish, outcomes


async def main(args: argparse.Namespace) -> int:
    limits = httpx.Limits(max_connections=args.concurrency * 2 + args.runs + 10)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=60, limits=limits) as client:
        headers = await login(client, args.user, args.password)
        hits_before = await metric_total(
            client, "cache_operations_total", cache="retrieval", result="hit"
        )
        miss_before = await metric_total(
            client, "cache_operations_total", cache="retrieval", result="miss"
        )
        reads = await read_path(client, headers, args.concurrency, args.duration)
        hits = (
            await metric_total(client, "cache_operations_total", cache="retrieval", result="hit")
            - hits_before
        )
        misses = (
            await metric_total(client, "cache_operations_total", cache="retrieval", result="miss")
            - miss_before
        )
        create, finish, outcomes = await workflows(client, headers, args.runs, args.run_timeout)

    report = {
        "base_url": args.base_url,
        "concurrency": args.concurrency,
        "phases": [reads.summary(), create.summary(), finish.summary()],
        "workflow_outcomes": outcomes,
        "runs_per_minute": round(outcomes.get("COMPLETED", 0) / finish.seconds * 60, 1)
        if finish.seconds
        else 0,
        "retrieval_cache": {
            "hits": hits,
            "misses": misses,
            "hit_ratio": round(hits / (hits + misses), 3) if hits + misses else None,
        },
    }
    print(json.dumps(report, indent=2))
    if args.json:
        with open(args.json, "w") as handle:
            json.dump(report, handle, indent=2)
    failed = reads.errors + create.errors + finish.errors
    return 1 if failed else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--user", default="operator@demo.local")
    parser.add_argument("--password", default=os.environ.get("SEED_DEMO_PASSWORD", "Demo#Pass123"))
    parser.add_argument(
        "--concurrency", type=int, default=20, help="parallel clients for the read path"
    )
    parser.add_argument("--duration", type=float, default=10.0, help="seconds for the read path")
    parser.add_argument("--runs", type=int, default=30, help="workflows to start at once")
    parser.add_argument(
        "--run-timeout", type=float, default=120.0, help="give up on a run after this"
    )
    parser.add_argument("--json", help="also write the report to this file")
    return parser.parse_args()


if __name__ == "__main__":  # pragma: no cover  (exercised by the load-test unit test via main())
    sys.exit(asyncio.run(main(parse_args())))
