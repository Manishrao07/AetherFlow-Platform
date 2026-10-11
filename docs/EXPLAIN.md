# Interview cheat-sheet

Read this the night before. Every answer is short enough to say out loud, and every answer points at
a file you can open. If you cannot explain something here in your own words, read the file it points
to until you can; that is the point of this document.

Contents: [2-minute pitch](#1-the-two-minute-pitch) ·
[Decisions in plain English](#2-major-decisions-in-plain-english) ·
[30 likely questions](#3-likely-interview-questions) ·
[Weak spots to own up to](#4-weak-spots-say-them-before-they-are-found) ·
[How AI tools were used](#5-how-i-used-ai-tools-and-what-i-verified-myself) ·
[If asked about X, open file Y](#6-if-asked-about-x-open-file-y)

---

## 1. The two-minute pitch

"I built a multi-agent workflow platform. You give it a goal in plain language, say *check the
incident policy, open a ticket and email ops*. A **Planner** agent turns that into a structured plan,
and the code validates it: the schema, the dependencies, whether the tools exist and whether *this
user* is allowed to use them. Anything risky, like sending an email, **pauses for a human
approval before a single step runs**. An approver sees the exact plan and inputs, and they can never
be the person who asked. Then an **Execution** agent runs the steps through a tool layer where every
call passes seven gates (right tool, in the plan, allowed for the agent, allowed for the user's
role, valid input, approved if risky, and inside a timeout), a **Retrieval** agent answers from a
knowledge base with citations, and a **Review** agent checks the result and can send the plan back.

The part I'm proudest of isn't the AI, it's the **reliability around it**. A run is an explicit
state machine stored in Postgres; every change writes a timeline event in the same transaction;
a worker holds a lease, so if it dies another picks the run up; and every tool attempt has an
idempotency key, so a retry or a crash never sends the email twice. The model sits behind a
provider-agnostic client with retries, a circuit breaker and cost tracking, and there's a
deterministic mock so the whole system runs, and is tested, without an API key.

Around that: JWT with refresh rotation and reuse detection, role-based access, an append-only audit
log, SSRF protection, Prometheus metrics and Grafana, tracing where the trace id is the run id, a
Streamlit UI that only talks to the REST API, 801 tests at about 98% coverage, and docs that record every
trade-off, including what I'd change at ten times the scale."

---

## 2. Major decisions in plain English

**Explicit state machine in Postgres.** A workflow can be in one of a handful of states and only
certain moves are legal. The rules live in one file, and every move is written together with an event
row, in one transaction. That gives a trustworthy timeline, safe restarts and easy testing.

**Plan, then approve, then execute.** The model proposes; the code checks; a human authorises risk.
Risk levels come from the tool's own metadata, not from the model, so the model cannot talk its way past
approval. Approval covers the whole plan *before* anything runs, so nothing half-executes and stalls.

**Leases and idempotency instead of "exactly once".** Distributed systems can't promise a task runs
exactly once, so I made running it twice harmless. A lease says who is working on a run; if the worker
dies the lease expires and another worker resumes. Every tool attempt has a key, and a finished attempt
is replayed from the database instead of re-executed.

**Postgres full-text search, not a vector database.** It answers policy-style questions well,
needs no extra service, and gives results I can assert on in tests. The search function is the seam
where pgvector would be added. I wrote the limitation down rather than hiding it.

**Roles read from the database on every request.** JWTs normally go stale: a demoted user keeps their
powers until the token expires. One cheap indexed query per request means deactivation and role
changes take effect immediately, at the cost of that lookup.

**Defence in depth on tools.** Seven gates, schema-validated inputs, timeouts, output caps, a
whitelist-only calculator (no `eval`), and an `http_fetch` that resolves the host, checks every
address is public, and connects to the address it checked, which defeats DNS rebinding.

**A mock LLM as a real provider.** Tests, CI, load tests and demos need to be deterministic and free.
The mock has hooks (`[force-bad-json]`, `[force-revision]`, `[force-reject]`) to trigger failure paths on
demand.

**Generic `app/core/`.** Config, security, audit, LLM client, observability and cache contain no workflow
words, so they can be lifted into another service. Domain code depends on core, never the reverse.

**Streamlit calling only the REST API.** No database access in the UI means authentication, RBAC, audit
and rate limits can't be bypassed from it. Role checks in the UI only hide buttons; the API enforces.

---

## 3. Likely interview questions

### Architecture and orchestration

**1. Why did you write your own orchestrator instead of LangGraph or Temporal?**
The hard parts of this problem are not LLM calls; they are legal transitions, approval pauses, retries
with idempotency, leases and recovery. A framework would hide exactly that. My engine is about 1,300 lines, tested
end to end, and each transition is a row in `run_events`. If runs lasted weeks or crossed many services I would
move to Temporal, and the store, queue and agents are the seams for that. *(tradeoffs.md #1)*

**2. How do you stop illegal state changes?**
`state_machine.py` holds the table of allowed moves for runs and steps; the store checks it before every
update and raises `InvalidTransitionError` otherwise. The update also uses optimistic locking on a `version`
column, so two actors changing the same run can't both win; the loser re-reads and retries. Unit tests walk
the whole table. *(orchestrator/state_machine.py, store.py)*

**3. What happens if a worker dies halfway through a run?**
The worker holds a lease on the run. When the lease expires, the sweeper (at start-up and every 15 s) re-queues
active runs, and returns interrupted steps to PENDING under the *same attempt number*. Because the idempotency
key is derived from run, step and attempt, a tool that had already finished is replayed from `tool_executions`,
not executed again. `tests/e2e/test_recovery.py` kills a worker mid-run.

**4. How do you make sure an email isn't sent twice?**
Three layers: the attempt key, replay of finished attempts from storage, and a unique key on the
`notification_outbox` table per workflow step, so even a bug above would hit a constraint. Retried *failed*
attempts get a new attempt number and key, and the earlier failures stay visible.

**5. How does the DAG execution work?**
Plan steps list `depends_on`. The engine runs every step whose dependencies have succeeded, concurrently, with a
semaphore (`MAX_PARALLEL_STEPS`). If a step fails after its retries, only its dependents are skipped; independent
branches finish. A failed run can be repaired by retrying the single failed step, which also un-skips its dependents.

**6. Why in-process asyncio workers rather than Celery?**
Fewer moving parts, and the work is I/O-bound. The queue is only a wake-up signal behind a `JobQueue` interface;
Postgres decides what's actually runnable, and leases make several workers safe. So it already scales out
(`--profile workers`), and Celery or SQS would be a drop-in improvement for back-pressure, not a correctness fix.

**7. Why a modular monolith?**
A state change, its event and an approval must be one atomic transaction; splitting into services would force
sagas and compensation for no benefit at this size. The module boundaries (core, services, orchestrator, agents,
tools) are enforced by imports, and the worker already deploys separately from the same image.

### AI and LLM integration

**8. How do you handle prompt injection?**
I assume it will happen and limit what it can do. Retrieved text is wrapped as untrusted data with an
unforgeable fence, and the Retrieval agent drops citations it didn't actually retrieve. More importantly, a model
can't *act*: plans are validated, tools are allow-listed per agent and per user role, risky steps need human
approval, and the Execution agent is deterministic: the approved plan, not model text, chooses tool and inputs.

**9. What stops the model from marking a dangerous step as low risk?**
Nothing it says matters. After parsing, the planner overwrites `risk_level` and `requires_approval` from the tool's
own metadata. The model can raise risk, never lower it. *(agents/planner.py)*

**10. What if the model returns invalid JSON?**
`complete_json` validates against a Pydantic model; if that fails it makes exactly one repair attempt that shows
the model the validation error. If that also fails it raises a retryable error, the cached bad response is evicted,
and the step retries with backoff. One repair, not a loop, because loops burn money on a model that isn't converging.

**11. How do retries and the circuit breaker interact?**
Order is breaker → retry → timeout → provider. Retries use exponential backoff with full jitter and honour
`Retry-After`; only retryable errors (timeouts, 429, 5xx, connection) are retried. After five consecutive failures
the breaker opens and calls fail fast for 30 s, then one probe decides whether to close it. This stops a dead
provider from tying up every worker. State is exported as a metric and shown in `/readyz`.

**12. How do you track LLM cost?**
Every attempt, including failures, is a row in `llm_calls` with tokens, latency, error and cost computed from
per-model prices in the environment. Totals appear in the run timeline and as Prometheus counters. With the mock
provider the price is zero, so cost shows $0.

**13. How do you test code that depends on an LLM?**
Deterministic mock provider with keyword rules per agent task, plus hooks to force bad JSON, revisions and
rejections. The Anthropic provider is tested against a stubbed SDK only; I say that plainly in the README because
it means no test proves answer quality on a real model.

**14. How would you evaluate real answer quality?**
Honestly, that's the gap: there's no eval harness. I'd build a small golden set of goals with expected tool usage and
cited sources, run it against the real provider nightly, and track pass rate, cost and latency per release, using the
`llm_calls` table and the review score as signals.

**15. How is conversation memory managed?**
Short-term: a sliding window plus a rolling summary within a token budget (`SHORT_TERM_TOKEN_BUDGET`); old
messages are folded into the summary once and the position stored, and if summarisation fails the request still works
with a truncated window. Long-term: per-user facts in Postgres, private by construction (the tools have no user-id
input), erasable on request.

### Data and search

**16. Why Postgres full-text instead of embeddings?**
For a policy knowledge base it's accurate enough, adds no service, and is deterministic to test. The cost is no
semantic matching. `search_chunks` is the seam: add an embedding column, rank by a hybrid score, nothing above
changes. *(tradeoffs.md #2)*

**17. Walk me through the schema and one index choice.**
Eighteen tables in roughly third normal form; UUID keys; statuses as VARCHAR+CHECK so adding a state is a one-line
change; JSONB only for plans and tool payloads. Example: `workflow_runs` has an index on `(user_id, created_at DESC)`
because the main query is "my newest runs"; documents and memories have GIN indexes on generated `tsvector` columns.
Every index is justified in `docs/database.md`.

**18. Why optimistic locking?**
Contention on one run is rare but real (a cancel racing a step finishing). A version column is cheap and
needs no long-held row locks; the loser gets a stale-data error and retries with fresh state.

**19. How is the audit log tamper-resistant?**
A trigger rejects UPDATE, DELETE and TRUNCATE, so even application bugs can't rewrite history. It's written in its own
transaction, so a denied or failed request is still recorded. It has no foreign keys, so deleting a user doesn't
cascade into the trail; it snapshots the actor's email.

### Security

**20. How does auth work, and what is the weakness of JWTs?**
Argon2id passwords; 15-minute HS256 access tokens with algorithm pinned and token *type* checked; 7-day refresh tokens
stored by `jti`, rotated every use, and reuse of a rotated token revokes the user's whole family (theft detection).
The classic weakness is stale claims, which I avoid by reading roles from the DB each request. What remains is that an
access token can't be revoked for up to 15 minutes.

**21. How is RBAC enforced?**
One table maps roles to named permissions; every route declares the permission it needs in its signature, so a route
without a guard is visible in review. Ownership rules sit in the services, and other people's resources return 404 rather
than 403 so ids can't be probed. Denials are audited.

**22. Explain separation of duties.**
The requester can't decide their own approval, even if they're an admin. It's checked in the service *and* by a CHECK
constraint (`decided_by <> requested_by`), so a bug can't skip it. The decision is an atomic conditional update, so two
approvers racing can't both succeed, and the second gets a 409. *(tests/e2e/test_approvals.py)*

**23. How do you prevent SSRF in `http_fetch`?**
Host allow-list first, then DNS resolution, then every resolved address must be public (no loopback, private, link-local
or metadata ranges), then the connection is made to the *validated IP* so DNS can't change between check and use.
Redirects are followed manually and re-validated; responses are size-capped. *(core/netguard.py)*

### Scale, performance, operations

**24. How would this handle 100× the load?**
API replicas are stateless; Redis shares rate limits. Workers scale separately and leases keep them safe. The first
bottleneck is Postgres write volume from events and LLM-call rows, so: partition `run_events`/`audit_logs` by month,
add a read replica for timelines, PgBouncer, and a real queue for wake-ups. I measured a 2.5× throughput gain just from
raising per-worker concurrency from 4 to 16 with a 0.5 s model. *(docs/performance.md)*

**25. What did you measure, and what didn't you?**
Measured on a dev sandbox, not production hardware: retrieval cache raised read throughput 66 % (180→298 req/s, p95
182→94 ms), and concurrency 4→16 gave 2.5× workflow throughput. Not measured: behaviour with a real provider's latency
distribution, or multi-node Postgres.

**26. How do you observe a run?**
Metrics with bounded labels (route templates, never ids, so cardinality can't explode); JSON logs with a request id;
traces where the trace id is the run id, so one trace holds a whole run across workers and restarts; a 22-panel Grafana
dashboard; and the per-run timeline in the UI, which comes from the same events the engine writes.

**27. Why your own tracing instead of OpenTelemetry?**
I wanted trace id = run id, which fits awkwardly with the SDK's ID generator, and I wanted no heavy dependency. It still
exports OTLP/HTTP JSON and understands `traceparent`. In a company already on OTel I'd switch to the SDK and keep the
run-id convention.

**28. What happens when Redis or the LLM is down?**
Redis down: the rate limiter fails open (logged) and caches degrade to misses. LLM down: calls time out, retry with
jitter, the breaker opens, steps fail with a clear error, `/readyz` shows it, and an operator can retry just the failed
step later. Database down: `/readyz` fails so load balancers stop routing, and in-flight runs recover through leases.

### Frontend and process

**29. Why Streamlit, and how does it stay secure?**
It was the fastest way to a complete, testable UI over the same REST API, and UI is a small part of the brief. Tokens live in
server-side session state, never cookies or URLs; the UI has no DB access; role checks only hide buttons. Replacing it with React
wouldn't touch the backend.

**30. How do you handle configuration and secrets?**
Everything is an environment variable, validated at start-up with Pydantic, so a bad value fails the boot, not a request. In
production a weak or placeholder `JWT_SECRET` is rejected, the seed script refuses to run, and there are no secrets or model
names in code (`.env.example` only).

---

### Research mode (added after the first version)

**How does deep research work?** The Planner only produces sub-questions; code turns them into a
DAG of research steps that run in parallel, followed by one synthesis step. Each research step
asks every source at once (documents, Wikipedia, arXiv, optionally web) and the model writes
findings that cite passage ids. The synthesizer merges them; the Review agent is the critic and
the existing revision loop is the corrective pass. It runs on the same engine, so approvals,
retries, tracing and audit come for free.

**Why not let the model design the research graph?** The shape never changes, so a model-built
graph only adds ways to fail. Code also owns citation numbers so the report cannot cite a source
that was never retrieved.

**Why is the embedding stored as a float array and not pgvector?** I could not run the pgvector
image where I built this, and the stock Postgres image keeps the setup unchanged. The search
interface is the same either way; moving to `vector` + HNSW is one migration. I say this openly
because it is a scaling limit (a scan per search), not a correctness problem.

**Does semantic search really understand meaning?** Not with the default offline embedder: it
hashes words (plus the synonym list), so it catches shared vocabulary and listed paraphrases.
With `EMBEDDING_PROVIDER=openai` and a model it is real semantic search. I have only tested the
remote path against a stub.

**What does the confidence score mean?** 50% the share of sub-questions with a grounded answer,
20% how many sources were cited (5 = full marks), 30% the Review agent's score. It is a
transparency aid, not a probability of being correct.

**Why are follow-up questions answered from the report only?** It stays grounded in sources the
reader can see, costs one call and cannot cause side effects. New facts need a new run.

**What stops web text from taking over the model?** It is fenced as untrusted data at every
stage (research, synthesis, follow-up); research steps have no write tools; research tools reach
only fixed hosts; the UI escapes links, images and HTML in reports.

## 4. Weak spots: say them before they are found

- **Keyword search**, not semantic. Seam exists; not built.
- **No live-model test or eval harness.** The Anthropic provider is tested against a stub.
- **Approvals are per plan**; the approver can't edit the plan.
- **Rate limiting is per IP**, fixed window; no per-account lockout; no MFA.
- **Single-process worker per replica**; scale-out relies on leases, not a managed queue.
- **SSE polls the events table** (0.5 s) rather than LISTEN/NOTIFY, and the UI polls every 2 s.
- **Docker images were not built in the sandbox where this was developed** (no Docker daemon there).
  Everything else ran for real; `docs/RUBRIC_CHECK.md` lists exactly what was and wasn't verified.
- **Cost is $0 on the mock** because it has no prices; real cost depends on the env price settings.

---

## 5. Engineering workflow and verification

**Implementation phases.** Built in eight systematic, reviewed phases:
foundation, security, LLM and tools, agents, orchestrator, observability, UI, and documentation.

**What the tests and checks proved (concrete, not vibes).**
- Strict typing (`mypy --strict`), `ruff`, and 801 tests with real Postgres, including a real-server end-to-end suite.
- Bugs that real runs exposed and the agent then fixed with regression tests: the redaction filter was masking
  `prompt_tokens`/`completion_tokens` in traces; a service returned an object bound to a closed DB session; the chat screen
  compared message roles in the wrong case; chat buttons disappeared on rerun; an expired session hid its own explanation.
  A performance claim in the docs that hadn't been measured was replaced with measured numbers.
- Docs checked against the code: the curl examples in `api.md` and the demo steps were executed against a running server,
  and corrected where responses differed from what was first written.

**What I must verify myself before the interview** (tick these off; do not skip):
- [ ] `docker compose up --build`, `make seed`, `make demo` on my own machine (Docker could not be run in the build
      sandbox, so images have not been built there).
- [ ] Read `orchestrator/engine.py`, `store.py` and `tools/executor.py` top to bottom and explain them without looking.
- [ ] Break something on purpose (kill the API mid-run; set `LLM_PROVIDER=anthropic` with a bad key) and watch it recover or fail
      cleanly.
- [ ] Run once with a real `ANTHROPIC_API_KEY` and `LLM_MODEL` to see real plans (never exercised in tests).
- [ ] Re-derive three answers above in my own words.

**If asked "did AI write this?"** Yes, with my direction, and I can show the process: phase plans, the bugs it got wrong,
and the tests that caught them. The design decisions in `tradeoffs.md` are the ones I can defend, and I know where they are weak.

---

## 6. If asked about X, open file Y

| Topic | Open |
|---|---|
| State machine / legal transitions | `app/orchestrator/state_machine.py`, `docs/diagrams/flow-state-machine.md` |
| Optimistic locking, events, leases | `app/orchestrator/store.py` |
| Plan → approval → DAG → review loop | `app/orchestrator/engine.py`, `docs/diagrams/sequence-workflow.md` |
| Approvals / separation of duties | `app/orchestrator/approvals.py`, migration `0001` (`ck_approvals_separation_of_duties`), `tests/e2e/test_approvals.py` |
| Crash recovery | `app/orchestrator/worker.py`, `store.py`, `tests/e2e/test_recovery.py` |
| Planner validation and risk override | `app/agents/planner.py`, `app/agents/plan.py` |
| Retrieval, citations, untrusted text | `app/agents/retrieval.py`, `app/core/untrusted.py` |
| Review rules | `app/agents/review.py` |
| JSON repair | `app/agents/base.py` (`complete_json`) |
| Mock LLM and test hooks | `app/core/llm/mock_provider.py`, `app/agents/mock_handlers.py` |
| Retry, backoff, breaker | `app/core/llm/retry.py`, `circuit_breaker.py`, `client.py`, `docs/diagrams/flow-retry-circuit-breaker.md` |
| Tool gates, idempotency | `app/tools/executor.py`, `app/tools/registry.py` |
| SSRF | `app/core/netguard.py`, `app/tools/builtin/http_fetch.py` |
| Calculator safety | `app/tools/builtin/calculator.py` |
| Auth, tokens, refresh rotation | `app/core/security.py`, `app/services/auth_service.py`, `docs/diagrams/sequence-login.md` |
| RBAC | `app/services/permissions.py`, `app/core/rbac.py`, `app/api/deps.py` |
| Audit log | `app/core/audit.py`, migration `0001` (trigger) |
| Redaction | `app/core/redaction.py` |
| Security headers, size limits, rate limit | `app/core/security_middleware.py`, `app/core/ratelimit.py` |
| Schema and indexes | `docs/database.md`, `docs/diagrams/er.md`, `app/models/` |
| Full-text search | `app/services/knowledge.py`, `app/services/chunking.py` |
| Memory | `app/memory/short_term.py`, `app/memory/long_term.py` |
| Caching | `app/core/cache.py`, `app/services/knowledge.py` |
| Metrics, tracing | `app/core/observability/`, `app/metrics.py`, `docs/diagrams/observability.md` |
| Dashboards | `deploy/grafana/dashboards/workflow-platform.json` |
| Load test and numbers | `scripts/load_test.py`, `docs/performance.md` |
| UI | `ui/streamlit_app.py`, `ui/api_client.py`, `ui/views/` |
| Config validation | `app/core/config.py`, `.env.example` |
| Wiring everything | `app/runtime.py`, `app/main.py` |
| Docker and compose | `Dockerfile`, `docker-compose.yml` |
| CI | `.github/workflows/ci.yml`, `.pre-commit-config.yaml` |
| Trade-offs | `docs/tradeoffs.md` |
| Deep research (planner, agents, synthesizer, confidence) | `app/agents/planner.py` (`_plan_deep`), `research.py`, `synthesizer.py` |
| Hybrid / semantic search | `app/services/knowledge.py`, `app/core/embeddings.py`, migration 0007 |
| Report export, follow-ups, similar runs | `app/services/reports.py`, `app/api/routers/workflows.py` |
| Groq provider and fallback | `app/core/llm/openai_compat_provider.py`, `fallback.py` |
| Research tools (web, Wikipedia, arXiv) | `app/tools/builtin/research.py` |
