# Demo script (about 9 minutes)

A minute-by-minute script for a screen recording. Everything here uses the default mock LLM, so it
needs no API key and gives the same result every time. The mock's answers are canned (they quote the
knowledge base); say so on camera. What you are demonstrating is the **platform around the model**.

## Before recording (2 minutes, off camera)

```bash
cd project1_workflow
docker compose up --build -d            # db, redis, migrations, API :8000, UI :8501
make seed                               # 4 demo users + 3 sample documents
curl -s localhost:8000/readyz           # {"status":"ready",...}
```

Optional extra for the last minute: `docker compose --profile observability up -d` (Prometheus :9090,
Grafana :3000, login admin/admin), then run `make demo` once so the dashboard has data.

Open three things: the UI at <http://localhost:8501>, a terminal in `project1_workflow/`, and
<http://localhost:8000/docs> in a background tab. Demo password for every account: `Demo#Pass123`.

| Account | Role |
|---|---|
| `operator@demo.local` | starts workflows, uploads documents |
| `approver@demo.local` | decides approvals |
| `admin@demo.local` | everything, plus users and audit log |
| `viewer@demo.local` | read only |

---

## 0:00 – 0:45 · The pitch (talk over the architecture diagram)

Open `docs/diagrams/architecture.md` (renders on GitHub).

> "This is a multi-agent workflow platform. You give it a goal in plain language. A **Planner**
> turns it into a validated plan, a **human approves anything risky**, an **Execution** agent runs the
> plan through a guarded tool layer, a **Retrieval** agent answers from a knowledge base with
> citations, and a **Review** agent checks the result. Everything is stored in Postgres, traced,
> metered and audited. The UI is a plain client of the REST API."

## 0:45 – 2:00 · Sign in and ask a question (Chat)

1. In the UI sign in as **operator@demo.local**.
2. **Chat** → expander *New conversation* in the sidebar → title `Demo` → **Create**.
3. Type in the chat box: `What is the refund window for digital goods?`
4. A "Working on it…" box shows the status moving PLANNING → EXECUTING → REVIEWING, then the
   conversation shows the answer, quoting the refund policy.

> "That message became a workflow run. The answer was retrieved from the knowledge base I seeded,
> with a citation, and the Review agent approved it before it was posted back here."

## 2:00 – 3:30 · What actually happened (Run timeline)

1. Click **Open run** on the message (or choose **Run timeline** in the sidebar).
2. Point at the header: status badge, review verdict and score, revisions, duration.
3. Point at the four numbers: **LLM calls, tokens in/out, cost, tool calls**.
4. Open *Plan and steps*: one step, `search_knowledge_base`; expand it to show inputs and output.
5. Show the **Tool calls** and **LLM calls** tables, then open the **Event log**.

> "Every state change is a row in `run_events`, written in the same database transaction as the
> change. This page is not a log somebody remembered to write; it's the actual history. Cost shows
> $0 because the mock model has no price; with a real model the per-million-token prices from the
> environment make this a real number."

## 3:30 – 5:45 · Human in the loop (New workflow → Approvals)

1. **New workflow** → click the example **Needs approval (sends an email)** → **Start workflow**.
2. You land on the timeline: status **AWAITING_APPROVAL**. Point out that the plan has three steps,
   the last one is HIGH risk, and **nothing has run yet**.

> "The model doesn't decide what's risky. The risk level comes from the tool's own metadata, so a
> model can't talk its way past the approval gate."

3. Click **Open approvals inbox**. As the operator you can see your request but there are **no
   approve buttons**: the page says someone else has to decide it.
4. **Sign out**, sign in as **approver@demo.local**, open **Approvals**.
5. Show the card: the goal, who asked, the plan table, and *Exact inputs the plan will use*.
6. Click **Reject** with an empty comment → "Please explain the rejection". Then type
   `checked the recipient` and click **Approve**.
7. **Run timeline** → the run executes all three steps and ends **COMPLETED**.

> "Four rules are visible here: the requester can't approve their own request (enforced in the API
> and by a database constraint), a rejection needs a reason, the approver sees the exact inputs,
> and the decision is atomic: two approvers clicking at once can't both win."

## 5:45 – 6:45 · Knowledge base (Documents)

1. Sign back in as **operator**. **Documents** → *Upload* tab → paste text:
   `Customers on the Enterprise plan get a 4-hour response for SEV1 tickets.` Title: `Enterprise SLA`.
2. **Ingest document** → "Ingested … as 1 searchable chunk(s)".
3. *Search* tab → `SEV1 response time` → the new passage appears with a citation.

> "Documents are chunked and stored in Postgres with a full-text index. I chose keyword search over a
> vector database on purpose, and the trade-offs doc explains why and where pgvector would plug in."

## 6:45 – 7:30 · Roles are enforced by the API (viewer, admin)

1. Sign in as **viewer@demo.local**: **New workflow** says viewers can't start workflows.
2. In the terminal, prove the API refuses even if the UI were bypassed:

   ```bash
   VIEW=$(curl -s localhost:8000/api/v1/auth/login -H 'Content-Type: application/json' \
     -d '{"email":"viewer@demo.local","password":"Demo#Pass123"}' | jq -r .access_token)
   curl -s -X POST localhost:8000/api/v1/workflows -H "Authorization: Bearer $VIEW" \
     -H 'Content-Type: application/json' -d '{"goal":"Calculate 1+1"}'
   ```

   Expected: HTTP 403 `forbidden`, with a `request_id`.

> "The UI hides what you can't do, but the API is the enforcement point."

3. Sign in as **admin@demo.local** → **Admin** → *Audit log*. Filter action `approval.approved`.
   Show who approved, when, from which IP. Then *Users*: roles can be changed here.

> "The audit log is append-only: a database trigger rejects updates and deletes."

## 7:30 – 8:30 · The same thing from the command line, and failure handling

In the terminal:

```bash
make demo
```

Talk over the output as it scrolls (it is the same flow, scripted, with checks):
grounded answer → **review sends the plan back for one revision** → approval pause with the
separation-of-duties refusal → role limits → **idempotency**: two requests with the same
`Idempotency-Key` return the same run → audit entries. It ends with
`Demo finished: every check above passed.`

> "Retries and crash recovery are safe for the same reason as the idempotency key: every tool attempt
> has its own key and a finished attempt is replayed from the database, never executed twice. I
> tested that by killing a worker mid-run (`tests/e2e/test_recovery.py`)."

## 8:30 – 9:00 · Observability and close

1. **Metrics** page in the UI: readiness, request counts, LLM tokens, cache hit ratio.
2. If the observability profile is up, open Grafana <http://localhost:3000> → *Workflow Platform*
   dashboard (22 panels).

> "801 tests at about 98% coverage, strict typing, CI, a load test with measured results in
> `docs/performance.md`, and every design decision with its alternatives is in `docs/tradeoffs.md`.
> Known limits are listed honestly in the README: keyword search, per-plan approvals, and a
> single-process worker that scales out by leases."

---

## Optional · Deep research (about 2 minutes)

1. **New workflow** → click the **Deep research** example (it ticks *Deep research mode*) → **Start workflow**.
2. On the run page the plan shows four *research* steps side by side and one *synthesizer* step.
   Say: "the Planner only wrote the questions; the graph is built by code, and the four
   researchers run in parallel."
3. When it completes, point at the **Confidence** metric, the cited report and its **Sources**
   list. Say: "citation numbers are assigned by code, so the report cannot cite a source that was
   never retrieved."
4. Click **Download PDF**. Type a follow-up (for example "When are digital goods refundable?") →
   **Ask**. Say: "answered from the report only, no new research."
5. Go back to **New workflow**, tick *Deep research mode*, repeat the same topic → the page
   offers the earlier run first. Say: "it only suggests; reusing old facts silently could be stale."
6. Without a model key everything above uses the mock LLM; with `LLM_PROVIDER=groq` the same
   screens show real model text.

## If something goes wrong on camera

| Symptom | Fix |
|---|---|
| Sign-in says invalid credentials | You skipped `make seed`. Run it and retry. |
| Chat says "Still working" | The run is just slow; open **Run timeline** and it refreshes by itself every 2 s. |
| `make demo` prints `DEMO FAILED … did you run make seed?` | Same: run `make seed`. |
| Port already in use | Stop the other process, or change the published ports in `docker-compose.yml`. |
| Want to start clean | `docker compose down -v` removes the database volume; then `up --build` and `make seed`. |
