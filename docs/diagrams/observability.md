# Observability: metrics, traces and logs

```mermaid
flowchart LR
    subgraph proc[API / worker process]
        MW[Request middleware<br/>request id, trace id, HTTP metrics]
        ENG[Orchestrator engine<br/>run / step spans, workflow metrics]
        LLM[LLM client<br/>llm spans, token + cost metrics, cache]
        TOOL[Tool executor<br/>tool spans, tool metrics]
        REG[(Prometheus registry<br/>in memory)]
        TR[Tracer<br/>span exporter]
        LOG[JSON logger<br/>request_id, run_id, step_id, trace_id]
    end
    MW --> REG
    ENG --> REG
    LLM --> REG
    TOOL --> REG
    MW --> TR
    ENG --> TR
    LLM --> TR
    TOOL --> TR
    MW --> LOG
    ENG --> LOG
    REG -- GET /metrics --> PROM[Prometheus<br/>scrapes every 5 s]
    PROM --> GRAF[Grafana<br/>provisioned dashboard]
    TR -- console exporter --> LOG
    TR -- OTLP/HTTP JSON --> COL[OpenTelemetry collector<br/>Jaeger / Tempo ...]
    LOG --> AGG[Log platform<br/>stdout in containers]
    DB[(Postgres<br/>run_events, llm_calls,<br/>tool_executions)] -. timeline API .-> UI[UI / API clients]
    ENG --> DB
    LLM --> DB
    TOOL --> DB
```

How the three signals connect:

| Question | Where to look | How it links |
|---|---|---|
| "Is the system healthy right now?" | Grafana / `/metrics` | route templates, statuses, tool and breaker names as labels - never ids |
| "What happened in run X?" | `GET /workflows/{id}/timeline` (database) | steps, redacted tool I/O, LLM tokens/cost, events; the audit trail |
| "Where did the time go in run X?" | spans, `trace_id` = the run id without dashes | `workflow.run` > `agent.*` > `step.run` > `tool.*` / `llm.*` |
| "What did request R do?" | logs filtered by `request_id` | the response header `X-Request-ID`; `traceparent` is honoured so a caller's trace continues |

Why metrics, the timeline and traces are separate: metrics are cheap and aggregate (they answer
*how many / how slow* and must stay low-cardinality); the timeline is durable business data that
the product itself shows to users; traces are an operator's debugging aid and may be sampled or dropped
without losing anything that matters.
