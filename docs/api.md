# API guide with curl examples

Base URL `http://localhost:8000/api/v1`. Interactive reference (all schemas, try-it-out):
<http://localhost:8000/docs>. Health and metrics live outside the prefix.

Run `make seed` first (or `docker compose run --rm api python scripts/seed.py`); the examples use the
demo accounts. They need [`jq`](https://jqlang.github.io/jq/) to pick tokens out of responses.

```bash
API=http://localhost:8000/api/v1
PASS='Demo#Pass123'

login() {   # usage: TOKEN=$(login operator)
  curl -s $API/auth/login -H 'Content-Type: application/json' \
       -d "{\"email\":\"$1@demo.local\",\"password\":\"$PASS\"}" | jq -r .access_token
}
OP=$(login operator); APPR=$(login approver); ADM=$(login admin); VIEW=$(login viewer)
```

**Conventions**

- Authenticate with `Authorization: Bearer <access token>` (15-minute lifetime).
- Every error has the same shape: `{"error": {"code", "message", "details", "request_id"}}`. Quote the
  `request_id` when reporting a problem; it is also in the `X-Request-ID` response header and in the logs.
- Lists are paginated with `?limit=` (max 100) and `?offset=` and return `{"items": [...], "total": N}` (exceptions: `GET /documents/search` and `GET /memory/search` return a plain array of ranked results).
- Other people's resources return **404**, not 403.
- Status codes: 200/201/202 success, 401 not signed in, 403 role not allowed, 404 not found or not
  yours, 409 conflict (already decided, wrong state), 413 body too large, 422 invalid input,
  429 rate limited (see `Retry-After`).

## Auth

| Call | Who | Notes |
|---|---|---|
| `POST /auth/register` | public | creates a `viewer` only |
| `POST /auth/login` | public | returns `access_token`, `refresh_token`, `expires_in` |
| `POST /auth/refresh` | public | rotates the refresh token; reusing an old one revokes the whole family |
| `POST /auth/logout` | public | revokes the refresh token |
| `GET /auth/me` | any user | profile and roles |

```bash
# register a new (viewer) account
curl -s $API/auth/register -H 'Content-Type: application/json' \
  -d '{"email":"new.person@example.com","full_name":"New Person","password":"correct-horse-9"}'

# log in and keep both tokens
TOKENS=$(curl -s $API/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"operator@demo.local","password":"Demo#Pass123"}')
REFRESH=$(echo "$TOKENS" | jq -r .refresh_token)

# who am I?
curl -s $API/auth/me -H "Authorization: Bearer $OP"

# get a new pair (the old refresh token stops working)
curl -s $API/auth/refresh -H 'Content-Type: application/json' -d "{\"refresh_token\":\"$REFRESH\"}"

# log out
curl -s -X POST $API/auth/logout -H 'Content-Type: application/json' -d "{\"refresh_token\":\"$REFRESH\"}"
```

## Users and audit log (admin)

```bash
curl -s "$API/users?limit=20" -H "Authorization: Bearer $ADM"

USER_ID=$(curl -s "$API/users" -H "Authorization: Bearer $ADM" | jq -r '.items[] | select(.email=="viewer@demo.local") | .id')
curl -s -X PUT $API/users/$USER_ID/roles -H "Authorization: Bearer $ADM" \
  -H 'Content-Type: application/json' -d '{"roles":["viewer","operator"]}'
curl -s -X PUT $API/users/$USER_ID/active -H "Authorization: Bearer $ADM" \
  -H 'Content-Type: application/json' -d '{"is_active":false}'

# append-only audit trail, filterable
curl -s "$API/audit-logs?action=approval.approved&limit=5" -H "Authorization: Bearer $ADM"
curl -s "$API/audit-logs?resource_type=workflow_run&limit=5" -H "Authorization: Bearer $ADM"
```

## Tools

```bash
# catalogue: input schema, risk level, whether it needs approval, and allowed_for_you
curl -s $API/tools -H "Authorization: Bearer $OP" | jq '.items[] | {name, risk_level, requires_approval, allowed_for_you}'
```

## Workflows (the core of the platform)

| Call | Who | Notes |
|---|---|---|
| `POST /workflows` | operator, admin | `202` with the run; `200` with the existing run when `Idempotency-Key` repeats |
| `GET /workflows`, `GET /workflows/{id}` | owner; approver and admin see all | `?status=`, `?everyone=true` for approvers/admins |
| `GET /workflows/{id}/timeline` | same | steps, tool calls, LLM calls, approvals, events, totals |
| `GET /workflows/{id}/events` | same | Server-Sent Events; resume with `Last-Event-ID` |
| `POST /workflows/{id}/cancel` | owner, admin | `409` if already finished |
| `POST /workflows/{id}/steps/{step_id}/retry` | owner, admin | only a FAILED step of a FAILED run |
| `GET /workflows/similar?goal=` | operator, admin (your own runs) | earlier completed runs on a similar topic, best first |
| `GET /workflows/{id}/report?format=md\|pdf` | same as read | `409` until COMPLETED |
| `POST /workflows/{id}/ask` | operator, admin | follow-up answered from the report only; `422` if the question is shorter than 3 characters |

```bash
# start a run (safe to repeat with the same key)
RUN=$(curl -s $API/workflows -H "Authorization: Bearer $OP" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: my-first-run' \
  -d '{"goal":"What is the refund window for digital goods?","max_revisions":2}' | jq -r .id)

curl -s $API/workflows/$RUN -H "Authorization: Bearer $OP" | jq '{status, review_verdict, result}'

# full picture: steps, tool calls, LLM calls (tokens, cost), approvals, events, totals
curl -s $API/workflows/$RUN/timeline -H "Authorization: Bearer $OP" | jq '{totals, steps: [.steps[] | {step_key, tool, status}]}'

# live events (Ctrl-C to stop); each event has an id you can resume from
curl -N $API/workflows/$RUN/events -H "Authorization: Bearer $OP"
curl -N $API/workflows/$RUN/events -H "Authorization: Bearer $OP" -H 'Last-Event-ID: 12'

# cancel, or retry a failed step
curl -s -X POST $API/workflows/$RUN/cancel -H "Authorization: Bearer $OP"
curl -s -X POST $API/workflows/$RUN/steps/$STEP_ID/retry -H "Authorization: Bearer $OP"

# a conversation-bound run posts its answer back into the conversation
curl -s $API/workflows -H "Authorization: Bearer $OP" -H 'Content-Type: application/json' \
  -d "{\"goal\":\"Calculate 1200 * 0.18\",\"conversation_id\":\"$CONV\"}"
```

### Deep research

```bash
# mode "deep": sub-questions -> parallel research -> cited report (goal may be "A vs B" to compare)
RUN=$(curl -s $API/workflows -H "Authorization: Bearer $OP" -H 'Content-Type: application/json' \
  -d '{"goal":"Deep research on the refund policy for digital goods","mode":"deep"}' | jq -r .id)

# report text + confidence once COMPLETED
curl -s $API/workflows/$RUN -H "Authorization: Bearer $OP" | jq -r '.result.summary, .result.confidence'
curl -s "$API/workflows/$RUN/report?format=pdf" -H "Authorization: Bearer $OP" -o report.pdf

# follow-up, answered from the report only
curl -s $API/workflows/$RUN/ask -H "Authorization: Bearer $OP" -H 'Content-Type: application/json' \
  -d '{"question":"When are digital goods refundable?"}' | jq

# did I already research something like this?
curl -s -G $API/workflows/similar --data-urlencode "goal=refund policy for digital goods" \
  -H "Authorization: Bearer $OP" | jq
```

Run states: `CREATED → PLANNING → [AWAITING_APPROVAL] → EXECUTING → REVIEWING → COMPLETED`
(or back to `PLANNING` on `NEEDS_REVISION`; `FAILED`/`CANCELLED` from most states). See the
[state machine](diagrams/flow-state-machine.md).

## Approvals

```bash
# a goal with a HIGH-risk step (sends a notification) pauses before anything runs
RUN=$(curl -s $API/workflows -H "Authorization: Bearer $OP" -H 'Content-Type: application/json' \
  -d '{"goal":"Check the SEV2 incident policy, open a ticket and email ops@example.com"}' | jq -r .id)

# the approver sees the goal and the exact plan and inputs
curl -s "$API/approvals?status=PENDING" -H "Authorization: Bearer $APPR" | jq '.items[] | {id, goal, requester_email, reason}'
AP=$(curl -s "$API/approvals?status=PENDING" -H "Authorization: Bearer $APPR" | jq -r '.items[0].id')

curl -s -X POST $API/approvals/$AP/approve -H "Authorization: Bearer $APPR" \
  -H 'Content-Type: application/json' -d '{"comment":"checked the recipient"}'
curl -s -X POST $API/approvals/$AP/reject  -H "Authorization: Bearer $APPR" \
  -H 'Content-Type: application/json' -d '{"comment":"wrong recipient"}'      # comment is required

# an operator has no approval permission -> 403. If the REQUESTER is an admin or approver, the
# separation-of-duties rule refuses them with 403 too. Deciding twice -> 409.
curl -s -X POST $API/approvals/$AP/approve -H "Authorization: Bearer $OP" -H 'Content-Type: application/json' -d '{}'
```

## Conversations and memory

```bash
CONV=$(curl -s $API/conversations -H "Authorization: Bearer $OP" -H 'Content-Type: application/json' \
  -d '{"title":"Incident questions"}' | jq -r .id)

curl -s $API/conversations -H "Authorization: Bearer $OP"                  # yours only, newest activity first
curl -s -X POST $API/conversations/$CONV/messages -H "Authorization: Bearer $OP" \
  -H 'Content-Type: application/json' -d '{"content":"We are on call this week."}'
curl -s "$API/conversations/$CONV/messages?limit=50" -H "Authorization: Bearer $OP"
curl -s -X POST $API/conversations/$CONV/context -H "Authorization: Bearer $OP"   # window agents see
curl -s -X POST $API/conversations/$CONV/archive -H "Authorization: Bearer $OP"   # new messages then get 409

# long-term memory: private per user
curl -s -X PUT $API/memory/report-format -H "Authorization: Bearer $OP" -H 'Content-Type: application/json' \
  -d '{"content":"Prefers weekly summaries","kind":"PREFERENCE","importance":3}'
curl -s "$API/memory/search?q=weekly" -H "Authorization: Bearer $OP"
curl -s -X DELETE $API/memory/report-format -H "Authorization: Bearer $OP"
curl -s -X DELETE $API/memory -H "Authorization: Bearer $OP"             # erase everything I have stored -> {"deleted": N}
```

## Documents (knowledge base)

```bash
# ingest text (chunked and indexed) -> {"document": {...}, "created": true};
# identical content is a no-op: 200 with "created": false instead of 201
curl -s $API/documents -H "Authorization: Bearer $OP" -H 'Content-Type: application/json' \
  -d '{"title":"On-call handbook","content":"Page the on-call engineer within 5 minutes for SEV1."}'

curl -s "$API/documents?limit=20" -H "Authorization: Bearer $VIEW"
curl -s "$API/documents/search?q=refund%20window&top_k=3" -H "Authorization: Bearer $VIEW" | jq '.[] | {citation, score}'
curl -s -X DELETE $API/documents/$DOC_ID -H "Authorization: Bearer $OP"      # owner or admin; chunks cascade
```

## Operations

```bash
curl -s localhost:8000/healthz        # {"status":"ok"}: process is alive
curl -s localhost:8000/readyz         # {"status":"ready","checks":{"database":true,"llm":true}}; 503 when not
curl -s localhost:8000/metrics        # Prometheus text; add -H "Authorization: Bearer $METRICS_TOKEN" if set
```

## Permissions at a glance

| Permission area | admin | approver | operator | viewer |
|---|:-:|:-:|:-:|:-:|
| Read workflows, documents, tools | ✔ | ✔ | ✔ | ✔ |
| See other users' runs | ✔ | ✔ | – | – |
| Start workflows, ingest documents, write memory | ✔ | – | ✔ | – |
| Decide approvals (never your own) | ✔ | ✔ | – | – |
| Manage users, read the audit log | ✔ | – | – | – |

The table is a summary; the source of truth is `app/services/permissions.py`.
