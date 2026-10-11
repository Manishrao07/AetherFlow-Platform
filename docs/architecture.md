# Architecture

The Multi-Agent Workflow Platform turns a plain-language goal ("check the incident policy, open a
ticket and email ops") into a **validated plan**, gets a **human to approve anything risky**,
**executes** the plan through a guarded tool layer, and has a **reviewer** check the result,
with every step stored, traced, metered and audited.

Diagrams referenced below live in [`docs/diagrams/`](diagrams/): [high-level](diagrams/architecture.md),
[deployment](diagrams/deployment.md), [components](diagrams/components.md), [ER](diagrams/er.md),
[state machine](diagrams/flow-state-machine.md), [workflow sequence](diagrams/sequence-workflow.md).

## 1. Context and goals

| Goal | What it means here |
|---|---|
| **Correct by construction** | A run is always in exactly one legal state; illegal transitions raise. Side effects happen at most once even when work is retried or a worker dies. |
| **Humans stay in control** | Plans with HIGH-risk steps pause *before anything runs*. The requester can never approve their own request. |
| **Safe tool use** | A model never gets to run arbitrary code or reach arbitrary hosts. Every tool call passes seven gates. |
| **Explainable** | Every plan, step, tool call, LLM call (tokens, latency, cost), approval and role change is stored and queryable. |
| **Runs anywhere** | `docker compose up --build`, and also with `LLM_PROVIDER=mock` so it needs no API key. |
| **Scales out** | Stateless API replicas plus separately scalable workers; Postgres is the only source of truth. |

Non-goals (stated honestly): multi-tenant isolation, semantic (vector) search, per-step approvals,
and a managed queue. Each has a documented seam; see [tradeoffs](tradeoffs.md) and the README limitations.

## 2. Components

See the [component diagram](diagrams/components.md).

| Component | Where | Responsibility |
|---|---|---|
| **Generic core** | `app/core/` | Config (validated at start-up), logging with redaction, JWT + argon2, RBAC, audit, error envelope, rate limiting, security middleware, DB engine, cache, LLM client (retry, circuit breaker, providers), metrics and tracing, SSRF guard. Contains no workflow vocabulary. |
| **API** | `app/api/` | Routers declare their permission in the function signature; schemas validate input; one error format. |
| **Services** | `app/services/` | Use-cases: auth, users, documents and chunking, conversations, workflows (create/cancel/retry/timeline), knowledge search, seeding. |
| **Orchestrator** | `app/orchestrator/` | State machine, store (transitions + events + leases), engine (plan → approval → DAG → review → revise), approvals, worker, standalone runner. |
| **Agents** | `app/agents/` | Planner, Retrieval, Execution, Review: each a class with typed input/output, its own prompt and tool allow-list. |
| **Tools** | `app/tools/` | Registry and executor (the seven gates) plus seven built-ins. |
| **Memory** | `app/memory/` | Short-term (window + rolling summary) and long-term (per-user facts) memory. |
| **UI** | `ui/` | Streamlit client of the REST API; no database access. |
| **Observability** | `app/core/observability/`, `deploy/` | Prometheus metrics, own tracing module, Grafana dashboard. |

## 3. Data flow: one workflow, end to end

See the full [sequence diagram](diagrams/sequence-workflow.md). In words:

1. `POST /workflows` validates the caller's role, inserts a `CREATED` run (idempotent on the
   `Idempotency-Key` header), **commits**, and only then enqueues a wake-up. (Enqueue-before-commit
   would let a worker look for a row that is not visible yet.)
2. A worker leases the run and moves it to `PLANNING`. The **Planner** asks the LLM for JSON; the
   plan is checked for schema, dependency cycles, unknown tools, tools the *user's role* may not
   use, and input validity. Risk and approval flags are **overwritten from the tool's own
   metadata**, so a model cannot label a dangerous step "low risk".
3. If any step is HIGH risk the run goes to `AWAITING_APPROVAL` and stops. An approver (not the
   requester) sees the exact plan and inputs and approves or rejects (comment required to reject).
   No decision before `APPROVAL_TTL_SECONDS` expires the approval and fails the run.
4. `EXECUTING`: steps form a DAG. Independent steps run concurrently (bounded by
   `MAX_PARALLEL_STEPS`). Each attempt has its own idempotency key and is stored in
   `tool_executions`. A failed step skips only its dependents.
5. `REVIEWING`: the **Review** agent returns a verdict and score. Code, not the model, enforces
   that `APPROVED` needs every step to have succeeded and `score >= REVIEW_MIN_SCORE`. A
   `NEEDS_REVISION` verdict goes back to planning, up to `max_revisions` times.
6. `COMPLETED` (or `FAILED` / `CANCELLED`). If the run belongs to a conversation, the answer is
   posted there as the assistant's message.

Every transition writes a `run_events` row **in the same transaction** as the state change. The
timeline, the SSE stream and the UI all read those events, so what you see is what happened.

### 3b. Deep research mode

`POST /workflows` with `"mode": "deep"` changes only the **planning** and the step agents, not the
engine. See the [sequence diagram](diagrams/sequence-deep-research.md).

1. The Planner asks the model for sub-questions only (`plan_deep`, capped by
   `DEEP_RESEARCH_MAX_QUESTIONS`). Code builds the DAG: one `research` step per question (all
   independent, so they run in parallel) and one `synthesizer` step depending on all of them.
   `Plan.tools` includes each research step's sources, so the registry's "tool is in the plan"
   gate still applies.
2. A **Research** step queries its sources at once through the tool layer (knowledge base,
   Wikipedia, arXiv, web search if a key is set). A failing source is recorded in
   `sources_failed`; only if every source fails does the step fail (retryable when any failure
   was transient). The model writes findings citing passage ids; unknown ids are dropped.
3. The **Synthesizer** gets all findings, with sources numbered globally by code. It writes the
   report; markers pointing at no real source are removed, cited sources are renumbered 1..k and
   a Sources list is appended.
4. **Review** judges the report; `NEEDS_REVISION` re-plans through the normal revision loop.
5. On approval the engine stores a **confidence** score (50% share of sub-questions answered,
   20% number of sources, 30% review score) next to the report.

Afterwards: `GET /workflows/{id}/report` (Markdown/PDF), `POST /workflows/{id}/ask` (answer from
the report only) and `GET /workflows/similar` (suggest earlier research; never reuses it).

**Hybrid search.** `search_chunks` runs the Postgres full-text query and, when an embedder is
configured, a cosine scan over stored chunk vectors, then merges the two rankings with
reciprocal-rank fusion. Vectors carry the embedder name; only matching ones are compared; a
startup backfill embeds new or stale chunks. See [trade-offs 15](tradeoffs.md).

## 4. Data model

See [`docs/database.md`](database.md) for tables, constraints and the reason for each index, and the
[ER diagram](diagrams/er.md). Highlights: UUID keys; enums and CHECK constraints for statuses;
optimistic locking (`version`) on `workflow_runs`; JSONB only for genuinely semi-structured payloads
(plans, tool inputs/outputs); a trigger that makes `audit_logs` append-only; a unique constraint on
`(user_id, idempotency_key)`; a generated `tsvector` column with a GIN index for full-text search.

## 5. Threat model

Assets: user accounts and roles, the approval decision, the knowledge base, tool side effects
(tickets, emails), the audit trail, LLM spend.

| Threat | Mitigation | Where |
|---|---|---|
| Password guessing / credential stuffing | argon2id, strict per-IP rate limit on auth routes, identical 401 for every failure reason (also equal time via a dummy hash), failures audited | `app/core/security.py`, `ratelimit.py`, `services/auth_service.py` |
| Stolen access token | 15 minute lifetime; roles re-read from the DB on every request so deactivation takes effect at once | `app/api/deps.py` |
| Stolen refresh token | rotation on every use; reuse of an old token revokes the user's whole token family | `services/auth_service.py` |
| Forged or confused tokens | algorithm pinned, issuer checked, token *type* checked (refresh ≠ access) | `app/core/security.py` |
| Privilege escalation | one role→permission table; guards declared per route; refused attempts audited; admins cannot lock themselves out | `services/permissions.py`, `core/rbac.py` |
| Probing other users' data | other people's runs/conversations/memory return **404**, not 403, so ids cannot be probed | `services/workflows.py`, `conversations.py` |
| **Prompt injection** via documents or tool output | retrieved text is fenced as *untrusted data* with an unforgeable delimiter; the model has no ability to act on its own: the plan is validated, tools are allow-listed and approval-gated | `core/untrusted.py`, `agents/retrieval.py`, `tools/executor.py` |
| A model "approving itself" or downgrading risk | risk/approval flags come from tool metadata, not the model; HIGH risk needs a human; reviewer verdicts are bounded by code | `agents/planner.py`, `agents/review.py` |
| Rogue approver / insider | requester cannot decide their own request (API **and** DB constraint); approvers see exact inputs; decisions are atomic and audited; approval covers one plan version | `orchestrator/approvals.py`, migrations |
| **SSRF** through `http_fetch` | host allow-list, public-IP check on every resolved address, connect to the validated IP (defeats DNS rebinding), redirects re-validated, size cap | `core/netguard.py`, `tools/builtin/http_fetch.py` |
| Code execution through `calculator` | AST whitelist, no `eval`, size/complexity limits | `tools/builtin/calculator.py` |
| Duplicate side effects (double email/ticket) | idempotency key per attempt; finished attempts are replayed from `tool_executions`; tickets/emails dedupe on a per-step key | `tools/executor.py` |
| Audit tampering | append-only trigger; written in its own transaction so failures are recorded even if the request rolls back | `core/audit.py`, migration 0001 |
| Secrets in logs, traces, audit rows | central redaction by key name and value pattern; token **counts** are explicitly allow-listed | `core/redaction.py` |
| Resource exhaustion | body-size limit (declared and streamed), rate limits, `MAX_CONCURRENT_RUNS`, per-tool and per-LLM timeouts, output caps, circuit breaker | `core/security_middleware.py`, `core/llm/` |
| SQL injection | SQLAlchemy parameterised queries only; user text is only ever a bound parameter to `plainto_tsquery` | `services/knowledge.py` |
| Insecure defaults reaching production | start-up validation: weak or placeholder `JWT_SECRET` is rejected when `ENVIRONMENT=production`; the seed script refuses to run in production | `core/config.py`, `scripts/seed.py` |
| **Untrusted text rendered in the UI** (goals, documents, model answers could carry Markdown links or images, a known prompt-injection exfiltration channel) | all such text goes through `plain()`, which escapes Markdown, neutralises HTML and breaks URL auto-linking; checked in a real browser: no images load, no request leaves for the attacker host, no disguised or clickable links (only `mailto:` for e-mail addresses) | `ui/format.py` (`plain`), `tests/ui/` |
| Clickjacking / sniffing / CORS abuse | security headers, HSTS in production, explicit CORS allow-list (never `*` with credentials) | `core/security_middleware.py`, `main.py` |

Research mode adds more untrusted text (web pages, Wikipedia, arXiv abstracts). Defences:
research tools can only reach fixed, configured hosts (the model controls the query text, never
the URL); arXiv XML is parsed with `defusedxml`; every passage is fenced as untrusted data before
any model sees it, and again before the synthesizer and the follow-up answerer; research and
synthesizer steps are LOW risk because they have no side effects (no write tools); the UI shows
reports through `report_markdown`, which keeps headings, lists and tables but escapes links,
images and HTML.

Out of scope / residual risk: no MFA, no per-account lockout (rate limit is per IP), keyword
search can be gamed by crafted documents (mitigated by fencing, not eliminated), and the
in-process rate limiter is per replica unless Redis is configured.

## 6. Scalability plan

| Layer | Today | Next step |
|---|---|---|
| **API** | Stateless; scale with replicas behind a load balancer. Rate limits shared through Redis. | Nothing to change. |
| **Workers** | In-process asyncio worker per API replica, **or** dedicated processes (`WORKER_ENABLED=false` on the API; `docker compose --profile workers up --scale worker=N`). A run is driven by exactly one worker via a **lease** in Postgres. | Swap the `JobQueue` implementation for Arq/Celery/SQS; leases and idempotency already make that safe. |
| **Concurrency** | `MAX_CONCURRENT_RUNS` runs per worker and `MAX_PARALLEL_STEPS` steps per run. Measured: 4 → 16 gave 2.5× throughput with a 0.5 s model ([performance](performance.md)). | Autoscale workers on queue depth (`workflow_runs_total`, run duration metrics). |
| **Database** | One Postgres; indexes on every query path ([database](database.md)); connection pool sized in config. | Read replica for list/timeline queries; partition `run_events` and `audit_logs` by month; PgBouncer. |
| **Search** | Postgres full-text search with GIN index; results cached and invalidated by a `kb:version` counter. | pgvector behind `search_chunks` (the seam is the function signature) for semantic search. |
| **LLM** | Circuit breaker + retry with full jitter; optional response cache; cost per call stored. | Per-tenant budgets and model routing. |
| **Caching** | Retrieval cache on, LLM cache opt-in; Redis if configured. Measured +66 % read throughput. | Per-key TTL tuning from hit-ratio metrics. |

## 7. Failure modes

| What fails | What happens | Recovery |
|---|---|---|
| LLM provider down or slow | Calls time out; retries with exponential backoff and full jitter; after repeated failures the **circuit breaker opens** and calls fail fast instead of piling up. `/readyz` reports the breaker state. | Step retried (`STEP_MAX_ATTEMPTS`); after that the run is `FAILED` with the error and an operator can **retry the failed step** from the UI or API. |
| LLM returns malformed JSON | One automatic repair attempt, then treated as a retryable error. Invalid cached responses are evicted. | Same as above. |
| Tool fails or times out | Attempt stored with error; retried only if the error is retryable; dependents skipped, independent steps continue. | Retry the step; earlier successful steps are not repeated. |
| Worker crashes mid-run | Its lease expires; the sweeper re-queues the run and returns interrupted steps to `PENDING` under the **same** attempt number, so the same idempotency key replays the stored result instead of repeating the side effect. | Automatic (`RUN_LEASE_SECONDS`, `SWEEP_INTERVAL_SECONDS`); tested in `tests/e2e/test_recovery.py`. |
| API restarts during an SSE stream | The client reconnects with `Last-Event-ID` and resumes from the event cursor. | Automatic. |
| Approval never answered | The approval expires after `APPROVAL_TTL_SECONDS`; the run is failed with a clear reason. | Start the workflow again. |
| Two people decide the same approval | Decisions are atomic; the loser gets 409. | None needed. |
| Duplicate submission (double click, client retry) | Same `Idempotency-Key` returns the original run. | None needed. |
| Redis unavailable | Rate limiter **fails open** (logged), caches degrade to misses; nothing returns an error because of it. | Restore Redis. |
| Database unavailable | `/readyz` fails (load balancers stop routing); requests return a generic 5xx; connection retries at start-up with a bounded wait. | Restore Postgres; in-flight runs recover through leases. |
| Optimistic-lock conflict | The store retries the transition with fresh data. | Automatic. |

## 8. Non-functional requirements → where in the code

| NFR | How it is met | Where |
|---|---|---|
| **Scalability** | Stateless API, lease-based workers, bounded concurrency, indexed queries, caching | `orchestrator/worker.py`, `runner.py`, `store.py`, `core/cache.py`, `docs/performance.md` |
| **Security** | Argon2, pinned JWT, RBAC, refresh rotation and reuse detection, SSRF guard, fenced untrusted text, approvals with separation of duties, hardening middleware, redaction | `core/security.py`, `core/rbac.py`, `core/netguard.py`, `core/untrusted.py`, `orchestrator/approvals.py`, `core/security_middleware.py` |
| **Observability** | Prometheus metrics (bounded labels), traces where trace id = run id, JSON logs with request ids, Grafana dashboard, per-call LLM cost | `core/observability/`, `app/metrics.py`, `deploy/`, [observability diagram](diagrams/observability.md) |
| **Resilience** | Timeouts everywhere, retry with jitter, circuit breaker, leases and crash recovery, idempotency, graceful shutdown, fail-open limiter | `core/llm/retry.py`, `circuit_breaker.py`, `orchestrator/engine.py`, `tools/executor.py` |
| **Maintainability** | Small modules with top docstrings and `WHY:` comments; strict mypy; ruff; generic core separate from domain; 801 tests at ~98 % coverage | whole repo, `pyproject.toml`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml` |
| **Auditability** | Append-only `audit_logs` in its own transaction; `run_events` timeline written with every transition; `llm_calls` and `tool_executions` per attempt | `core/audit.py`, `orchestrator/store.py`, `models/` |
| **Performance** | Retrieval cache (+66 % throughput), parallel DAG steps, bounded pools, measured with a repeatable load test | `core/cache.py`, `orchestrator/engine.py`, `scripts/load_test.py`, `docs/performance.md` |
| **Testability** | Deterministic mock LLM with test hooks; real Postgres in tests; real-server end-to-end tests | `core/llm/mock_provider.py`, `agents/mock_handlers.py`, `tests/` |

## 9. Deployment and operations

See the [deployment diagram](diagrams/deployment.md). Configuration is entirely environment
variables (table in the README), validated at start-up so a bad setting fails the boot rather than a
request. Health: `/healthz` (process alive), `/readyz` (database and LLM breaker). Cloud notes:
the same images run on any container platform; use a managed Postgres and Redis, put the API
behind a TLS-terminating load balancer (set `TRUST_PROXY_HEADERS` only when behind one you control),
inject `JWT_SECRET` and `ANTHROPIC_API_KEY` from a secret manager, run migrations as a one-off job
(as the `migrate` service does), and scale workers separately from the API.
