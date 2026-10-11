# Deployment diagram (Docker Compose)

`docker compose up --build` starts the solid boxes. Dashed boxes are opt-in profiles.

```mermaid
flowchart TB
    subgraph host["Host machine"]
        P8501(["localhost:8501"])
        P8000(["localhost:8000"])
        P9090(["localhost:9090"])
        P3000(["localhost:3000"])
    end

    subgraph net["Docker network: backend (bridge)"]
        ui["ui<br/>image target: ui<br/>streamlit :8501<br/>non-root uid 10002"]
        api["api<br/>image target: runtime<br/>uvicorn :8000<br/>non-root uid 10001<br/>runs the worker in-process"]
        migrate["migrate<br/>one-shot: alembic upgrade head"]
        db[("db<br/>postgres:16-alpine<br/>no published port")]
        redis[("redis<br/>redis:7-alpine<br/>no published port")]
        worker["worker (profile: workers)<br/>python -m app.orchestrator.runner<br/>scale with --scale worker=N"]
        prom["prometheus (profile: observability)"]
        graf["grafana (profile: observability)<br/>provisioned dashboard"]
    end

    pgdata[/"volume: pgdata"/]
    promdata[/"volume: promdata"/]
    grafanadata[/"volume: grafanadata"/]

    P8501 --> ui
    P8000 --> api
    P9090 --> prom
    P3000 --> graf

    ui -- "HTTP, API_BASE_URL=http://api:8000" --> api
    migrate --> db
    api -. "waits for migrate to finish" .-> migrate
    api --> db
    api --> redis
    worker --> db
    worker --> redis
    prom -- "scrape api:8000/metrics" --> api
    graf --> prom
    db --- pgdata
    prom --- promdata
    graf --- grafanadata

    style worker stroke-dasharray: 5 5
    style prom stroke-dasharray: 5 5
    style graf stroke-dasharray: 5 5
```

| Service | Image / target | Port | Notes |
|---|---|---|---|
| `db` | `postgres:16-alpine` | none published | Only containers on `backend` can reach it. Volume `pgdata`. |
| `redis` | `redis:7-alpine` | none published | Rate-limit counters and cache. |
| `migrate` | `runtime` | none | Runs `alembic upgrade head`, then exits; `api` waits for it. |
| `api` | `runtime` | 8000 | Non-root, health check on `/healthz`. |
| `ui` | `ui` (separate, no API dependencies) | 8501 | Starts after `api` is healthy. |
| `worker` | `runtime` | none | Optional extra orchestrators; set `WORKER_ENABLED=false` on the API. |
| `prometheus`, `grafana` | upstream images | 9090, 3000 | Dashboard and datasource are provisioned from `deploy/`. |
