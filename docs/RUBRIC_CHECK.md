# Rubric check

Each grading criterion and bonus item, mapped to the code or document that satisfies it, with the
honest gaps listed. Test figures: **801 tests passing, 97.8 % coverage** (gate 80 %), `ruff` and
`mypy --strict` clean.

## Grading criteria

| Criterion (weight) | Where it is satisfied | Evidence |
|---|---|---|
| **Architecture (20)** | Layered modular monolith: generic `app/core/` → `services` → `orchestrator` → `agents` → `tools`; explicit state machine in Postgres; plan → approval → DAG execution → review loop; lease-based workers and crash recovery; separate UI process | [architecture.md](architecture.md), [tradeoffs.md](tradeoffs.md) (14 ADRs), 15 diagrams in 12 files, [diagrams/](diagrams/README.md); `app/orchestrator/` (`state_machine.py`, `store.py`, `engine.py`, `approvals.py`, `worker.py`, `runner.py`); `tests/e2e/` (50+ tests incl. recovery, cancel, parallel, revision loop) |
| **Code quality (15)** | `mypy --strict` on `app`, `scripts` and `ui`; `ruff` (E,F,I,B,UP,S,C4,SIM,RUF); every module has a top docstring and `WHY:` comments on decisions; small focused modules; generic core kept free of domain words; pre-commit hooks | `pyproject.toml`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`; 801 tests across `tests/unit`, `integration`, `e2e`, `ui` |
| **Database design (15)** | 18 tables in ~3NF; UUID keys; status as VARCHAR + CHECK; FK delete rules chosen per relationship; optimistic locking (`workflow_runs.version`); DB-enforced rules (separation of duties, decision completeness, attempts ≤ max); append-only audit trigger; unique idempotency keys; generated `tsvector` + GIN; partial index for recoverable runs; 5 Alembic migrations (and `alembic check` shows no drift) | [database.md](database.md) (every index explained), [diagrams/er.md](diagrams/er.md), `app/models/`, `migrations/versions/`, `tests/integration/` |
| **AI / LLM integration (15)** | Provider-agnostic client (Anthropic + deterministic mock); model name from `LLM_MODEL`; retry with full jitter, hard timeout, circuit breaker; tokens, latency, cost per call stored; four agents with typed contracts and tool allow-lists; planner validates schema, cycles, tools, role and inputs and **overrides risk flags from tool metadata**; one-shot JSON repair; untrusted-text fencing and citation checking; review enforced by code; short- and long-term memory; 7 tools | `app/core/llm/`, `app/agents/`, `app/tools/`, `app/memory/`; [diagrams/flow-agents.md](diagrams/flow-agents.md), [flow-retry-circuit-breaker.md](diagrams/flow-retry-circuit-breaker.md) |
| **Security (10)** | argon2id; pinned JWT with type check; refresh rotation + reuse detection; roles re-read per request; RBAC table; separation of duties (API + DB); 404-not-403; append-only audit; redaction; SSRF guard; AST calculator; rate limiting; body limits; security headers; fail-fast config; untrusted text escaped in the UI | threat model in [architecture.md §5](architecture.md#5-threat-model); `app/core/security.py`, `rbac.py`, `netguard.py`, `redaction.py`, `security_middleware.py`, `untrusted.py`; `tests/integration/test_auth*.py`, `tests/unit/` |
| **Scalability (10)** | Stateless API; separate worker processes (`--profile workers`); leases; bounded concurrency; indexed queries; Redis-shared rate limits and caches; measured gains | [architecture.md §6](architecture.md#6-scalability-plan), [performance.md](performance.md) (retrieval cache +66 % throughput; concurrency 4→16 gave 2.5×), `scripts/load_test.py` |
| **Documentation (10)** | README (features, architecture, prerequisites, 3-command quickstart, env table, demo credentials, tests, structure, API overview, screenshots, limitations, roadmap); architecture, trade-offs, API (curl examples executed against a live server), database, performance, demo script, interview cheat-sheet; 15 Mermaid diagrams (12 files) with SVG/PNG exports | `README.md`, `docs/` |
| **UI / UX (5)** | Streamlit: sign-in, chat, new workflow, live run timeline (status colours, tool calls, LLM calls, tokens, cost), approvals inbox, documents, metrics, admin (users, audit log); role-aware navigation; clear error and empty states | `ui/`, screenshots in the README, `tests/ui/` (client, pages under `AppTest`, real-server end-to-end) |

## Bonus items

| Bonus | Status | Where |
|---|---|---|
| **CI/CD** | Done (assumes this folder is the repo root) | `.github/workflows/ci.yml`: ruff, format check, mypy, pytest with Postgres service, Docker builds of both images. The workflow assumes this project folder is the repository root; nest it in a bigger repo and it must be moved up with a `working-directory` (noted in the file). The workflow itself has not run on GitHub yet. |
| **Caching** | Done | `app/core/cache.py` (in-memory LRU+TTL and Redis, metered); retrieval cache invalidated by a version counter; opt-in LLM response cache with eviction of invalid output |
| **Automated testing** | Done | 801 tests: unit, integration (real Postgres), e2e (real worker, mock LLM, recovery, retries), UI (client, `AppTest` pages, real-server), demo script test |
| **Monitoring dashboards** | Done | `deploy/grafana/dashboards/workflow-platform.json` (22 panels, a test keeps queries in sync with real metrics), Prometheus config, `docker compose --profile observability up` |
| **Advanced analytics** | **Partial** | Per-run totals (LLM calls, tokens, cost, tool calls) in the timeline and Prometheus counters for cost/tokens/latency/approval wait. There are **no dedicated analytics endpoints** (cost per user per day, approval turnaround reports) yet. |
| **Agent workflows** | Done (plus deep research: parallel research agents, synthesizer, critic loop - [diagram](diagrams/sequence-deep-research.md)) | Plan → approval → DAG → review → revise; retry of a single failed step; conversation-bound runs; `docs/diagrams/sequence-workflow.md` |
| **Performance optimisation** | Done, measured | Caching, parallel DAG steps, bounded pools, repeatable load test with results and caveats in [performance.md](performance.md) |
| **Cloud deployment notes** | **Partial** | Guidance only ([architecture.md §9](architecture.md#9-deployment-and-operations)): managed Postgres/Redis, secrets, migrations as a job, scaling workers. No Terraform/Helm. |

## Honest gaps and limitations

- **Docker images were not built or run in the environment where this was developed.** The sandbox had a
  Docker daemon but no access to Docker Hub (organisation egress policy), so base images could not be
  pulled. What *was* verified: `docker compose config` accepts the compose file, and the Dockerfile steps
  were reproduced natively (see below). The first real `docker compose up --build` (run on a Mac) built
  both images and exposed one real bug: files copied with private host modes (0600) were unreadable by the
  non-root container user (`PermissionError: 'pyproject.toml'` in `migrate`). Fixed with
  `chmod -R a+rX /srv` in each stage. After the fix, `docker compose up --build` on the Mac ran all five migrations, started
  the API (healthy) and the UI. `make seed` (4 users, 3 documents) and `make demo` (every check passed) were then run inside the real containers.
- **The Anthropic provider is tested against a stubbed SDK only**; no live model call was made. Plan and answer
  *quality* on a real model is unmeasured, and there is no evaluation harness.
- **Semantic search is approximate.** Hybrid search combines full-text with a cosine scan over `real[]` embeddings (not the pgvector extension - it could not be run where this was built; trade-offs 15). The default offline embedder understands shared words and the listed synonyms only; a real embedding model needs `EMBEDDING_PROVIDER=openai` and a key.
- **Groq, Tavily and the remote embedder were never called live**: they are tested against stubbed HTTP. Wikipedia and arXiv were exercised live from the Docker container for deep research (a real run returned a report citing a Wikipedia page). Report quality on a real model is unmeasured.
- **No LLM-judge evaluation harness.** The Review agent scores each run, but there is no golden-set evaluation.
- Approvals are **per plan**; the approver cannot edit the plan.
- Rate limiting is per IP with a fixed window; no per-account lockout; no MFA.
- The worker is asyncio in-process (or a separate process); scale-out relies on leases, not a managed queue.
- The UI polls (2 s) rather than streaming; browser behaviour was checked manually with Chromium
  (including a hostile-markdown attack test) but there is no automated browser suite in the repo.
- Load-test figures come from a development sandbox, not production hardware, and use a simulated 0.5 s model.
- Cost shows $0 with the mock provider (no prices); set `LLM_INPUT_COST_PER_MTOK` / `LLM_OUTPUT_COST_PER_MTOK` for real numbers.

## Verification matrix

Done at the end of phase 8 from a **fresh copy of only `project1_workflow/`** (`cp -a` to an empty
directory), with new Python 3.12 virtual environments and brand-new databases:

| Check | Result | How |
|---|---|---|
| Lint, format, strict typing | Pass | `ruff check`, `ruff format --check`, `mypy` (120 source files) |
| Migrations from empty DB, no drift | Pass | `alembic upgrade head` (0001 → 0005), then `alembic check`: "No new upgrade operations detected" |
| Full test suite | **Pass: 801 tests, 97.8 % coverage (re-run after the research and search additions; the clean-copy check below was done on the 696-test version)** | `pytest` with `-e ".[dev]"` against a new `wf_clean_test` database |
| API image contents | Pass | Only what the Dockerfile `runtime` stage copies (`pyproject.toml`, `app`, `alembic.ini`, `migrations`, `scripts`) plus `pip install .`: `app` imports, Streamlit is not needed |
| `make seed` equivalent | Pass | `scripts/seed.py` on the migrated DB: "4 users, 3 documents created" |
| `make demo` equivalent | Pass | `scripts/demo.py` against that API: every check passes, audit counts match (4 workflow.created, 1 approval.approved, 3 access.denied) |
| UI image contents | Pass | Only `ui/` + `ui/requirements.txt` installed: Streamlit runs, `import app` fails (the UI cannot touch the backend code) |
| UI in a real browser | Pass | Chromium: sign in as operator, send a chat message, cited answer from the knowledge base shown |
| `docker compose config` | Pass | Docker itself validated the compose file (all profiles) |
| Hostile Markdown in goals/documents/answers | Pass | Chromium: no `<img>` loaded, no request to the attacker host, no clickable URLs (only `mailto:` links) |
| curl examples in `api.md` | Pass, after corrections | Executed against a live server; fixed the `/tools` response shape and the approval refusal wording |

**Not verified (and why):**

| Not verified | Why | What to do |
|---|---|---|
| Docker image builds in the sandbox | The sandbox had a Docker daemon but could not reach Docker Hub (egress policy returned 403), so base images could not be pulled. I did not try to work around that. | Run `docker compose up --build`, `make seed`, `make demo` once on your machine; CI also builds both images. |
| Anthropic provider against the real API | No key, and no live call is made in tests | Run once with `LLM_PROVIDER=anthropic`, `LLM_MODEL`, `ANTHROPIC_API_KEY`. |
| Grafana/Prometheus containers | Same registry restriction | `docker compose --profile observability up`; the dashboard queries are covered by a test against the real metric names. |

