# Database design

PostgreSQL 16, SQLAlchemy 2.x (async), Alembic. Diagram: [`diagrams/er.md`](diagrams/er.md).

## Principles

| Decision | Why |
|---|---|
| **3NF** | Roles in `roles`/`user_roles`; plan dependencies in `step_dependencies` (not a JSON list) so the DB guarantees they are real steps and they can be indexed. |
| **UUID primary keys** | Safe to expose in URLs (no enumeration), no central sequence. |
| **Status = `VARCHAR` + `CHECK`** (not native ENUM) | Adding a state is a one-line constraint change; native enums need `ALTER TYPE` and cannot drop values. The Python `StrEnum` stays the single source of truth. |
| **JSONB only where data is semi-structured** | `plan`, `inputs`, `output`, tool `input/output`, audit `details`. Everything queried or constrained is a real column. |
| **Optimistic locking** (`workflow_runs.version`) | Two workers/requests can't silently overwrite each other; the loser gets `StaleDataError` and re-reads. Tested. |
| **Database-enforced rules** | Separation of duties (`decided_by <> requested_by`), approval decisions must record who/when, no self-dependency, attempts <= max, score in [0,1]. A bug in service code still cannot violate them. |
| **`refresh_tokens` keyed by JWT `jti`** | Lets refresh tokens rotate, be revoked on logout, and detect theft (a reused token revokes the whole family). No secret is stored. |
| **`tickets` / `notification_outbox` with unique `idempotency_key`** | Our mocked external systems are real tables, so demos and tests can verify side effects; the unique key (one per workflow STEP) makes retries and crash-replays create at most one ticket / one email. |
| **Append-only `audit_logs`** | Trigger rejects UPDATE/DELETE/TRUNCATE. No FKs (an `ON DELETE SET NULL` would itself be a blocked UPDATE, and the trail must outlive users), so it snapshots `actor_email`. Production should also grant the app role INSERT/SELECT only. |
| **Generated `tsvector` columns** | Postgres keeps the full-text index in sync; application code cannot forget. |

## Foreign-key delete behaviour

| Relationship | ON DELETE | Reason |
|---|---|---|
| user -> conversations / runs / documents / memories | CASCADE | Deleting a user (privacy) removes their data. |
| conversation -> messages | CASCADE | History belongs to the conversation. |
| conversation -> workflow_runs, run -> messages | SET NULL | Archiving/deleting chat must not destroy the run audit trail or chat history. |
| run -> steps / approvals / llm_calls | CASCADE | Trace data has no meaning without its run. |
| step -> llm_calls | SET NULL | Keep cost data if a step row is replaced. |
| approvals.requested_by / decided_by -> users | RESTRICT | Can't delete a user out from under an approval decision. |
| user_roles.role_id -> roles | RESTRICT | Can't delete a role in use. |

## Indexes and the query each one serves

| Index | Query path |
|---|---|
| `uq_users_email_lower` (unique, `lower(email)`) | Login lookup; prevents `Bob@x` / `bob@x` duplicates. |
| `ix_user_roles_role_id` | "All users with role X" (admin page). PK covers user -> roles. |
| `ix_conversations_user_status_updated` | List my active conversations, most recent first. |
| `ix_messages_conversation_created` | Load a conversation's history in order (short-term memory window). |
| `ix_messages_run_id` | Find messages produced by a run; needed by the SET NULL FK. |
| `ix_workflow_runs_user_created` (`created_at DESC`) | Paginated "my workflows", newest first. |
| `ix_workflow_runs_conversation_id` | Runs in a conversation; FK support. |
| `ix_workflow_runs_recoverable` (**partial**: CREATED/PLANNING/EXECUTING/REVIEWING) | Crash-recovery scan (lease missing or expired). Excludes terminal runs, so it stays tiny. |
| `ix_refresh_tokens_user_id` | Revoke / inspect all refresh tokens of a user (reuse detection, deactivation). |
| `uq_tickets_idempotency_key`, `uq_notification_outbox_idempotency_key` | Dedupe side effects on retry / replay. |
| `ix_tickets_user_created`, `ix_tickets_run_id`, `ix_notification_outbox_user_created`, `ix_notification_outbox_run_id` | "My tickets / emails, newest first" and "what did run X do?"; FK support. |
| `uq_workflow_runs_user_idempotency_key` | Client retry of "create workflow" returns the same run. |
| `uq_workflow_steps_run_revision_step_key`, `ix_workflow_steps_run_position` | Timeline and scheduler: steps of a run in order; a revised plan re-uses keys (`s1`) in a new revision without losing history. |
| `ix_run_events_run_id_id` | SSE and timeline: `WHERE run_id = ? AND id > ?` is an index range scan. |
| `ix_step_dependencies_depends_on` | "Which steps unblock when this step finishes?" |
| `uq_tool_executions_idempotency_key` | Replayed job can't repeat a side effect. |
| `ix_tool_executions_step_id` | Tool calls for a step (timeline). |
| `ix_tool_executions_tool_status_created` | Tool failure-rate analytics. |
| `ix_llm_calls_run_created`, `ix_llm_calls_step_id` | LLM calls per run / step (timeline, cost per run). |
| `ix_llm_calls_created_at` | Cost / token dashboards over time windows. |
| `ix_approvals_pending_expires` (**partial**: PENDING) | Approvals inbox and the expiry sweeper. |
| `ix_approvals_run_id`, `ix_approvals_step_id` | Approvals for a run / step; FK support. |
| `uq_documents_owner_sha256` | Idempotent ingestion (same content twice is ignored). |
| `uq_document_chunks_document_index` | Ordered chunks; no duplicate chunk numbers. |
| `ix_document_chunks_search` (GIN) | Full-text retrieval `search_vector @@ query`. |
| `ix_memories_search` (GIN), `uq_memories_user_key` | Relevance search and upsert of a user's memories. |
| `ix_audit_logs_created_at` (`DESC`) | Admin audit page, newest first. |
| `ix_audit_logs_actor_created` | Everything one user did. |
| `ix_audit_logs_resource` | Full history of one run / document / approval. |
| `ix_audit_logs_action_created` | Filter by action (e.g. all logins). |

## Migrations

`0001_initial_schema` creates the core tables; `0002_refresh_tokens` adds token tracking; `0003_tool_side_effect_tables` adds tickets and the notification outbox; `0004` adds the `memory` LLM caller; `0005_orchestrator_support` adds the lease, step revisions and `run_events`. 0001 creates all tables, seeds the four roles (reference
data), and installs the audit triggers. `alembic check` reports no drift between models and
migration, and `downgrade base` then `upgrade head` round-trips cleanly (both verified).
In Docker a one-shot `migrate` service runs `alembic upgrade head` before the API starts, so
several API replicas never race to migrate.

## Phase 4 notes

- `llm_calls.agent` accepts `memory` (migration 0004) so the cost of summarising chat history is
  attributed to the conversation-summary service, not to one of the four plan agents.
  `workflow_steps.agent` deliberately does NOT accept it: a plan step can only be run by the
  retrieval or execution agent.
- `messages.created_at` is written with `clock_timestamp()` (not the transaction-frozen `now()`)
  so messages inserted in one transaction keep their order; `ix_messages_conversation_created`
  serves both the history endpoint and the short-term window.
- Document ingestion relies on `uq_documents_owner_sha256`: re-uploading identical content is
  detected by the application and guaranteed by the database.

## Phase 5 notes (migration 0005)

- **`run_events`** is the run's append-only event log. A row is written *in the same
  transaction* as each state change, so the log can never disagree with the run. Its identity
  `id` is the SSE cursor (`Last-Event-ID`): any API replica can serve any client by reading
  `id > cursor`, with no in-memory fan-out to lose on restart. `created_at` uses
  `clock_timestamp()` so events in one transaction keep their order.
- **`workflow_runs.lease_owner / lease_expires_at`**: a worker takes a lease before driving a
  run and renews it while working. A run in an active state whose lease is missing or expired is
  an orphan and gets re-queued. The claim is a single conditional `UPDATE`, so two workers can
  never both own a run.
- **`workflow_steps.revision`**: when the Review agent asks for a revision the Planner makes a
  new plan and its steps get `revision + 1`. Old steps stay in the table, so the timeline shows
  the whole history. The unique key is `(run_id, revision, step_key)`.
- Crash recovery sets a step that was `RUNNING` back to `PENDING` with `attempts - 1`, so the
  retry re-uses the same attempt number and therefore the same tool idempotency key.
