# Design decisions and trade-offs (ADR style)

Each entry: **choice**, **alternatives considered**, **why**, **consequences** (including what it
costs and when I would change it). Status for all: accepted.

| # | Decision | Short reason |
|---|---|---|
| 1 | Custom orchestrator, not LangGraph / Temporal | The state machine, approvals and recovery *are* the assignment; small enough to own and explain |
| 2 | Postgres full-text search, not pgvector or a vector DB | No extra service; honest keyword search now, a seam for vectors later |
| 3 | In-process asyncio worker behind a queue interface, not Celery | Zero extra infrastructure; leases make scaling out safe anyway |
| 4 | JWT access tokens + rotating refresh tokens, not server sessions | Stateless API replicas; roles still re-read from the DB each request |
| 5 | Async end to end (FastAPI, SQLAlchemy async) | Work is I/O-bound (LLM, DB, HTTP) |
| 6 | Modular monolith, not microservices | One team, one deploy, strong transactions; clean seams for later splits |
| 7 | Streamlit UI, not React | Fastest way to a complete, testable UI over the same REST API |
| 8 | Postgres as the single source of truth for run state | Atomic state + events; crash recovery without a second store |
| 9 | Plan-level approval, not per-step | One decision on exactly what will run; simpler to audit |
| 10 | Role-based access control with a permission table | Four roles are enough; permissions stay explicit |
| 11 | Own tracing module, not the OpenTelemetry SDK | Trace id = run id; no heavy dependency; OTLP export still available |
| 12 | Deterministic mock LLM as a first-class provider | Reproducible tests and demos with no API key |
| 13 | UI polls instead of using SSE | Streamlit is request/response; the API still offers SSE |
| 14 | Idempotency keys on both workflow creation and every tool attempt | Retries and crash recovery must never repeat a side effect |

---

## 1. Custom orchestrator vs LangGraph / Temporal

- **Choice:** an explicit state machine (`app/orchestrator/state_machine.py`), a store that applies
  transitions with optimistic locking, and an engine that runs the plan as a DAG.
- **Alternatives:** LangGraph (graph of LLM nodes with checkpoints); Temporal (durable workflow
  engine); a plain script of function calls.
- **Why:** the hard parts here are *non-LLM*: legal transitions, human approval pauses, retries
  with idempotency, leases and recovery. Using a framework would hide exactly what I need to show
  and explain, and Temporal needs its own cluster. The custom version is about 1,300 lines, fully tested,
  and every transition is a row in `run_events`.
- **Consequences:** I own the bugs (leases, retries, recovery were all tested explicitly). No
  built-in workflow versioning, timers UI or multi-language workers. If runs needed to last weeks
  or span many services, Temporal would be the right move; the engine's boundaries (store, queue,
  agents) are where it would plug in.

## 2. Full-text search vs pgvector vs a vector database

- **Choice:** `tsvector` generated column + GIN index, ranked with `ts_rank`, with citations.
- **Alternatives:** pgvector (embeddings in Postgres); Pinecone/Weaviate/Qdrant.
- **Why:** it needs no embedding model, no extra service and gives deterministic, testable
  results (the mock LLM and tests rely on that). Citations point at real chunk ids. For a policy
  knowledge base, keyword search with good chunking answers most questions.
- **Consequences (original choice):** no semantic matching ("refund" will not find "money back"); see section 15 for the hybrid search added later. The seam is
  `search_chunks` in `app/services/knowledge.py` (wrapped by `KnowledgeSearch`, which adds the cache): add an embedding column, hybrid
  rank, and nothing above it changes. A separate vector DB would add a consistency problem
  (two stores to keep in sync) that I only want when scale demands it.

## 3. asyncio worker vs Celery / Arq

- **Choice:** a worker inside the API process (or the same code as a standalone runner), behind a
  small `JobQueue` interface. The queue only says "look at run X"; Postgres decides.
- **Alternatives:** Celery + Redis/RabbitMQ; Arq; SQS.
- **Why:** fewer moving parts to run and demo, and LLM-bound work is I/O, which asyncio handles
  well. Because a run is guarded by a lease row, several workers can run at once without a broker
  guaranteeing exactly-once delivery.
- **Consequences:** a single process limits CPU-heavy work (none here) and queue features such as
  priorities and delayed jobs are limited to what the sweeper does. Scale-out works today
  (`--profile workers`); a managed queue would improve wake-up latency and back-pressure.

## 4. JWT + refresh rotation vs server-side sessions

- **Choice:** 15-minute HS256 access tokens, 7-day refresh tokens stored by `jti`, rotated on use,
  with reuse detection revoking the whole family.
- **Alternatives:** opaque session ids in Redis; OAuth/OIDC with an external provider.
- **Why:** stateless verification suits many API replicas, and rotation limits what a stolen
  token can do. The usual JWT weakness (stale roles) is removed by reading roles from the
  database on every request, which also makes deactivation immediate.
- **Consequences:** one extra indexed lookup per request; access tokens cannot be revoked before
  expiry (15 minutes). HS256 means one shared secret; for multiple services I would move to RS256
  or an identity provider.

## 5. Async vs sync

- **Choice:** async FastAPI and SQLAlchemy 2 with asyncpg.
- **Alternatives:** sync Flask/Django with thread pools.
- **Why:** each run waits on the model and the database most of the time; async lets one process
  hold many runs and parallel steps cheaply.
- **Consequences:** discipline is needed (no blocking calls in handlers; greenlet-aware coverage
  config; stricter test fixtures). The Streamlit UI and demo script stay synchronous on purpose.

## 6. Modular monolith vs microservices

- **Choice:** one deployable with enforced layering (`core` generic; `api` → `services` →
  `orchestrator` → `agents` → `tools`), plus a second small deployable for the UI.
- **Alternatives:** separate planner, executor and retrieval services.
- **Why:** state transitions, events and approvals must be atomic; across services they would need
  sagas. A monolith keeps that a single database transaction.
- **Consequences:** one scaling unit (mitigated by running workers as separate processes of the same
  image). The seams (`JobQueue`, `KnowledgeSearch`, `LLMClient`, tool registry) are where I would cut
  first.

## 7. Streamlit vs React

- **Choice:** Streamlit, calling only the REST API.
- **Alternatives:** React/Next.js SPA.
- **Why:** UI is 5 % of the grade and the backend is the substance. Streamlit gave all required
  screens, live updates and automated page tests (`AppTest`) quickly.
- **Consequences:** less control over layout and no true push updates. Because the UI is a plain
  API client, replacing it with React later does not touch the backend.

## 8. Postgres as the source of truth for run state

- **Choice:** run, step, event, approval and lease state live in Postgres; Redis holds only
  disposable data (rate counters, caches).
- **Alternatives:** keep state in memory/Redis and persist at the end; event sourcing.
- **Why:** a state change and its event are written atomically, so the timeline can never disagree
  with reality, and a crashed worker loses nothing.
- **Consequences:** more writes per run (one event per transition) and index maintenance;
  acceptable at this scale and partitionable later.

## 9. Plan-level approvals

- **Choice:** one approval per plan that contains a HIGH-risk step, taken *before* anything runs.
- **Alternatives:** approve each risky step when it is reached.
- **Why:** the approver sees the whole plan and the exact inputs once, and nothing partially
  executes and then stalls. Approval is tied to a plan version, so a revised plan needs a new one.
- **Consequences:** the approver cannot edit the plan, only accept or reject; per-step approval is
  a documented extension (`approvals.kind` already allows `STEP`).

## 10. RBAC with an explicit permission table

- **Choice:** four roles (admin, approver, operator, viewer) mapped to named permissions in one file;
  routes declare the permission they need.
- **Alternatives:** per-resource ACLs; ABAC/policy engine (OPA).
- **Why:** the rules fit in one readable table that I can explain and test completely. Ownership
  rules (see only your own runs; approvers and admins see all) sit in the services.
- **Consequences:** coarse-grained; no per-document sharing or tenants.

## 11. Own tracing module vs OpenTelemetry SDK

- **Choice:** a small tracing module with ContextVar nesting, console and OTLP/HTTP-JSON
  exporters and W3C `traceparent` support.
- **Alternatives:** the OpenTelemetry Python SDK with auto-instrumentation.
- **Why:** I wanted the trace id of a run to *be* the run id so a whole run (across workers and
  restarts) is one trace; that is awkward through the SDK's ID generators and adds many
  dependencies. Spans are still exportable to any OTLP collector.
- **Consequences:** no auto-instrumentation of libraries and less battle-testing. If the platform
  joined a larger OTel estate I would switch to the SDK and keep the run-id convention.

## 12. Mock LLM as a first-class provider

- **Choice:** `LLM_PROVIDER=mock` (default) answers every agent task deterministically; the
  Anthropic provider sits behind the same interface with the model name from `LLM_MODEL`.
- **Why:** tests, CI, demos and load tests must not need a key or money, and failure paths
  (`[force-bad-json]`, `[force-revision]`, `[force-reject]`) need to be reproducible.
- **Consequences:** the mock's answers are canned, so quality of real answers is not measured by
  the tests. The Anthropic provider is covered against a stubbed SDK only, which is stated in the
  README as a limitation.

## 13. UI polling vs SSE

- **Choice:** the run timeline refreshes with a 2-second Streamlit fragment; the API also serves
  Server-Sent Events (`/workflows/{id}/events`, resumable) for other clients.
- **Why:** Streamlit scripts are request/response, so a long-lived stream does not fit; polling a
  cheap read endpoint is simple and works behind any proxy.
- **Consequences:** up to 2 seconds of latency and one extra request per tick per open viewer.

## 14. Idempotency at two levels

- **Choice:** `Idempotency-Key` on workflow creation (unique per user) and a derived key per tool
  attempt (run, step, attempt).
- **Why:** clients retry on timeouts and workers die mid-step; both must be safe. A finished
  attempt is replayed from storage rather than re-executed.
- **Consequences:** tool authors must make external side effects dedupe on the key (tickets and
  emails do); truly non-idempotent third-party calls would need that service's own idempotency
  support.


## 15. Embeddings as `real[]` in the application vs pgvector

- **Choice:** each chunk stores a float array (`document_chunks.embedding`) and the name of the
  model that made it. Search runs the keyword query and a cosine scan over the stored vectors,
  then merges both rankings with reciprocal-rank fusion.
- **Alternatives:** pgvector (`vector` column + HNSW index); a dedicated vector database.
- **Why:** pgvector needs a different Postgres image and an extension. I could not run that image
  where this was developed, and the stock image keeps `docker compose up` unchanged. A plain
  array works everywhere, is fully covered by tests against real Postgres, and the interface
  (`Embedder`, `search_chunks`) does not change when the storage does.
- **Consequences:** the scan is O(chunks) per search (capped by `SEMANTIC_MAX_CANDIDATES`, and
  results are cached). Fine for thousands of chunks, not for millions. Upgrade path: one
  migration to `vector(N)` + HNSW, and replace the Python cosine loop with `ORDER BY embedding
  <=> :q`. `embedding_model` prevents mixing vectors from different models; a startup backfill
  re-embeds anything stale.
- **Honest limit:** the default `hash` embedder is feature hashing plus the synonym list. It
  catches shared vocabulary and listed paraphrases, not arbitrary meaning. Real semantics need
  `EMBEDDING_PROVIDER=openai` with a model.

## 16. Deep research as a plan shape on the same engine

- **Choice:** deep research is a run `mode`, not a second engine. The Planner asks the model for
  sub-questions only; code builds the DAG (N research steps in parallel, one synthesis step).
  Research and synthesizer are ordinary step agents, so approvals, retries, leases, tracing,
  cost tracking and the audit log apply with no extra code.
- **Alternatives:** a separate research service; LangGraph; letting the model design the graph.
- **Why:** the shape is always the same, so a model-designed graph only adds failure modes. Code
  also owns the citation numbers: the model never renumbers sources, and invented `[n]` markers
  are removed before the report is shown.
- **Consequences:** one fixed shape (no iterative "search again if the answer is thin" loop). The
  Review agent plus the existing revision loop is the corrective pass. If a sub-question's
  sources all fail, that step fails and the run follows the normal retry/revision rules.

## 17. Follow-up questions answered from the report only

- **Choice:** `POST /workflows/{id}/ask` sends the finished report (fenced as untrusted data) and
  the question to the model; no new tools or searches.
- **Why:** the answer stays grounded in sources the reader has already seen, costs one small
  call and cannot trigger side effects. "Similar past research" is likewise a suggestion the user
  accepts or ignores - never silently reused, because old facts go stale.
- **Consequences:** a follow-up that needs new facts must start a new run.
