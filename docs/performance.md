# Performance, caching and load-test results

All numbers below were measured on the development sandbox (2 vCPU, Postgres 16 on the same
machine, Python 3.13, ONE uvicorn process with the in-process worker, `LLM_PROVIDER=mock`).
They show relative effects and where the time goes; they are not a capacity promise for real
hardware or a real LLM. Reproduce with `scripts/load_test.py` (see the end of this file).

## What is cached, and why it is safe

| Cache | Key | TTL | Invalidation | Default |
|---|---|---|---|---|
| **Retrieval** (`KnowledgeSearch`) | `kb:<version>:sha256(normalised query, top_k)` | `RETRIEVAL_CACHE_TTL_SECONDS` = 60 | `ingest` / `delete` bump `kb:version`, so old entries are never read again | on (`CACHE_ENABLED`) |
| **LLM responses** (`LLMClient`) | `sha256(provider, model, task, system prompt, messages, max_tokens)` | `LLM_CACHE_TTL_SECONDS` = 3600 | the agent calls `evict` when an answer fails validation, so a malformed answer is never served twice | **off** (`LLM_CACHE_ENABLED`) |

- **Backend:** `REDIS_URL` set -> Redis (shared by every API/worker replica, including the version
  counter). Unset -> an in-process LRU+TTL cache (`CACHE_MAX_ENTRIES`). Same interface
  (`app/core/cache.py`), so nothing else changes.
- **A cache must never take the service down:** a Redis error is a miss on read and a no-op on
  write; a corrupt entry is a miss. Hits and misses are exported as
  `cache_operations_total{cache,result}` (Grafana panel "Cache hit ratio").
- **Retrieval results are shared between users** because the knowledge base is readable by every
  role; per-user data (memories, conversations) is never cached.
- **Why the LLM cache is opt-in:** a real model is not deterministic, so serving an hour-old answer
  is a product decision (cost control, demos, regression runs), not a free optimisation. On a hit
  the provider and the circuit breaker are not touched, so cached prompts keep working during a
  provider outage. The usage row is stored with `cached = true`, zero tokens and zero cost, so cost
  totals show what was really paid.
- **Staleness bound with the in-process backend:** another replica's `kb:version` bump is invisible
  here, so a search can be at most `RETRIEVAL_CACHE_TTL_SECONDS` out of date. Use Redis if that
  matters.

## Results

Load shape: 20 concurrent clients on `GET /documents/search` for 8 s, then 40 workflows started at
the same moment and polled to completion (3 goal templates; each run = plan + retrieval/tool step +
review = 3 LLM calls). Zero errors in every run; every run ended `COMPLETED`.

### 1. Retrieval cache (read path, instant mock LLM)

| | requests/s | p50 | p95 | p99 | cache hit ratio |
|---|---|---|---|---|---|
| cache **off** (`CACHE_ENABLED=false`) | 180 | 98 ms | 182 ms | 244 ms | - |
| cache **on** | 298 | 63 ms | 94 ms | 139 ms | 99.7 % |

The cache removes the full-text query: **+66 % throughput, p95 latency roughly halved.** The
remaining ~3.3 ms per request on one core is request handling itself (JWT check, a role lookup in
the database because roles are read fresh on purpose, JSON encoding) - which is why caching a
single query can not make it much faster than this.

### 2. Workflow throughput vs. worker concurrency (mock LLM with 0.5 s simulated latency)

With an instant mock the run is pure orchestration cost, which is small. A real model takes
seconds per call, so `MOCK_LLM_LATENCY_SECONDS=0.5` was added to make the effect visible.

| scenario | runs/min | end-to-end p50 | end-to-end p95 | batch finished in |
|---|---|---|---|---|
| instant mock, `MAX_CONCURRENT_RUNS=4` | 303 | 5.9 s | 7.9 s | 7.9 s |
| 0.5 s LLM, `MAX_CONCURRENT_RUNS=4` | 110 | 13.1 s | 21.6 s | 21.7 s |
| 0.5 s LLM, `MAX_CONCURRENT_RUNS=16` | 272 | 6.5 s | 8.6 s | 8.8 s |
| 0.5 s LLM, 16 runs + `LLM_CACHE_ENABLED=true` | 366 | 5.0 s | 6.5 s | 6.6 s |

Reading the table:

- **LLM-bound work scales with concurrency, not CPU.** Runs spend almost all their time waiting on
  the model, so going from 4 to 16 concurrent runs gave **2.5x the throughput** on the same process.
  (`MAX_CONCURRENT_RUNS` is the number of runs one process drives at once; the model rate limit is
  the real ceiling, which is why it is configurable.)
- **The LLM cache helps most when requests repeat** (here 3 distinct goals across 40 runs): +35 %
  runs/min and no model spend for the repeats.
- **`POST /workflows` accepts work in ~0.3 s p50 when 40 arrive in the same instant.** That is the
  single process sharing 2 cores with the worker; in the compose setup the API and workers can be
  separated (`WORKER_ENABLED=false` + `--profile workers`) so accepting work never competes with
  running it.
- **End-to-end times include the load script's 100 ms polling interval and the queue wait** when
  more runs are started than there are worker slots.

## Where the time goes (one run, measured from its trace)

One run of "What is the refund window for digital goods?" on an idle server with a
0.5 s simulated model latency (`TRACING_EXPORTER=console`, spans of one trace id):

| span | ms |
|---|---|
| `llm.plan` (inside `agent.planner`) | 506 |
| `tool.search_knowledge_base` | 7 |
| `llm.retrieve_answer` (inside `agent.retrieval`) | 505 |
| `llm.review` (inside `agent.review`) | 507 |
| **`workflow.run` (whole run)** | **1614** |

The three model calls are 1518 ms = **94 % of the run**; the one tool call is 7 ms; the remaining
~90 ms is orchestration - the state transitions between steps (each is a committed transaction with
an event row - the price of being resumable and auditable) plus the planning and persistence work.
With a real model the `llm.*` share only grows, which is why the design spends its effort on
retries, the breaker, caching and parallelism *around* the model instead of micro-optimising Python.

## Scaling path (what to change when one process is not enough)

1. **More workers, same code:** `docker compose --profile workers up --scale worker=N` with
   `WORKER_ENABLED=false` on the API. Postgres leases guarantee one driver per run; no broker needed.
2. **Shared cache:** set `REDIS_URL` (already in compose) so replicas share cache entries and
   invalidation.
3. **Stateless API replicas** behind a load balancer: SSE reads events from Postgres by cursor, so any
   replica can serve any client.
4. **Read replicas** for the timeline/list endpoints; they only read.
5. **Swap the queue:** implement `JobQueue.enqueue` on Celery/Arq/SQS; `WorkflowEngine.drive(run_id)`
   stays the unit of work.
6. **Search:** pgvector for semantic retrieval, a dedicated vector DB beyond that
   (`search_chunks` is the only function that changes).

## Reproduce

```bash
# terminal 1: a server without the rate limiter (it would answer 429 to a load test)
RATE_LIMIT_ENABLED=false MAX_CONCURRENT_RUNS=16 MOCK_LLM_LATENCY_SECONDS=0.5 \
  uvicorn app.main:create_app --factory --port 8000
# terminal 2
python scripts/load_test.py --base-url http://localhost:8000 --concurrency 20 --duration 8 --runs 40 --json results.json
```

The script prints a JSON report (per-phase p50/p95/p99, runs/min, retrieval-cache hit ratio read
from `/metrics`) and exits non-zero if any request failed, so it can gate CI.

Honest limits: single machine, a mock model, one run per scenario (no confidence intervals), and the
load generator shares the CPU with the server. Treat the ratios as meaningful and the absolute numbers
as indicative.
