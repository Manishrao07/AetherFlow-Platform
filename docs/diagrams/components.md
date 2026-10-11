# Component diagram (inside the API process)

Arrows read "depends on / calls". `app/core/` is generic (no workflow vocabulary) and could be
lifted into another service unchanged; everything outside it is specific to this platform.

```mermaid
flowchart TB
    subgraph core["app/core  (generic building blocks)"]
        cfg["config<br/>validated at start-up"]
        sec["security + rbac<br/>argon2, JWT, permissions"]
        aud["audit<br/>append-only, own transaction"]
        red["redaction"]
        err["errors<br/>one error envelope"]
        rl["ratelimit + security_middleware"]
        cache["cache<br/>memory / Redis, metered"]
        llmc["llm/<br/>client, retry, circuit breaker,<br/>mock + Anthropic providers"]
        obs["observability/<br/>metrics, tracing, middleware"]
        db["db/<br/>engine, session"]
        net["netguard<br/>SSRF checks"]
    end

    subgraph domain["Platform (domain-specific)"]
        api["api/<br/>routers, schemas, deps (guards)"]
        svc["services/<br/>auth, users, documents,<br/>conversations, workflows, knowledge"]
        orch["orchestrator/<br/>state_machine, store, engine,<br/>approvals, worker, runner"]
        agents["agents/<br/>planner, retrieval, execution, review"]
        tools["tools/<br/>registry, executor, 7 built-ins"]
        mem["memory/<br/>short-term window,<br/>long-term facts"]
        models["models/<br/>18 SQLAlchemy models"]
        rt["runtime.py<br/>wires everything once"]
    end

    ui["ui/ (separate process)<br/>REST client only"]

    ui -. "HTTP" .-> api
    api --> svc
    api --> sec
    api --> aud
    api --> err
    svc --> models
    svc --> mem
    api --> orch
    orch --> agents
    orch --> models
    agents --> llmc
    agents --> tools
    agents --> mem
    tools --> net
    tools --> models
    models --> db
    llmc --> obs
    orch --> obs
    rt --> cfg
    rt --> orch
    rt --> tools
    rt --> llmc
    rt --> cache
    sec --> red
    aud --> red
    api --> rl
```
