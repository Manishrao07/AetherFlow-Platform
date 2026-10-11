# High-level architecture

Who talks to whom, at the level you would draw on a whiteboard. Everything a user does goes
through the REST API; the web UI has no database access.

```mermaid
flowchart LR
    subgraph Clients
        U["Browser<br/>(operator / approver / admin / viewer)"]
        UI["Streamlit UI<br/>ui/ :8501"]
        CLI["curl / scripts<br/>(demo.py, load_test.py)"]
    end

    subgraph API["FastAPI service  (app/)  :8000"]
        MW["Middleware<br/>request id, security headers,<br/>size limit, rate limit, metrics"]
        R["Routers + RBAC guards<br/>auth, workflows, approvals,<br/>documents, memory, admin"]
        SVC["Services<br/>users, documents, conversations,<br/>workflows, knowledge search"]
        ORCH["Orchestrator<br/>state machine, DAG engine,<br/>approvals, worker, sweeper"]
        AG["Agents<br/>Planner, Retrieval,<br/>Execution, Review"]
        TOOLS["Tool registry + executor<br/>7 gates, timeouts, idempotency"]
        LLM["LLM client<br/>breaker, retry, timeout, cost"]
    end

    PG[("PostgreSQL 16<br/>source of truth:<br/>runs, steps, events, leases,<br/>approvals, documents (FTS),<br/>memory, audit log")]
    RD[("Redis<br/>rate limits,<br/>cache (optional)")]
    ANT["Anthropic API<br/>(LLM_PROVIDER=anthropic)"]
    MOCK["Mock provider<br/>(LLM_PROVIDER=mock, default)"]
    PROM["Prometheus + Grafana<br/>(profile: observability)"]

    U --> UI
    UI -- "HTTP + JWT" --> MW
    CLI -- "HTTP + JWT" --> MW
    MW --> R --> SVC
    R --> ORCH
    ORCH --> AG
    AG --> LLM
    AG --> TOOLS
    LLM --> ANT
    LLM --> MOCK
    SVC --> PG
    ORCH --> PG
    TOOLS --> PG
    MW --> RD
    PROM -- "scrape /metrics" --> MW
```

**Reading guide**

- The orchestrator is the only component that changes a run's state, and it does so through one
  store that writes the new state and a timeline event in the same database transaction.
- Agents never call each other; the orchestrator calls them in order and passes typed objects.
- Redis is optional: without `REDIS_URL` the rate limiter and caches run in-process.
