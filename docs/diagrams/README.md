# Diagrams

Mermaid sources (`*.md`, rendered by GitHub) with exported images in [`img/`](img/) as SVG and PNG
(regenerate with `make diagrams`).

| Diagram | Source | Images |
|---|---|---|
| High-level architecture | [architecture.md](architecture.md) | [svg](img/architecture.svg) · [png](img/architecture.png) |
| Deployment (containers, network, volumes, ports) | [deployment.md](deployment.md) | [svg](img/deployment.svg) · [png](img/deployment.png) |
| Components | [components.md](components.md) | [svg](img/components.svg) · [png](img/components.png) |
| Database ER | [er.md](er.md) | [svg](img/er.svg) · [png](img/er.png) |
| Sequence: login, refresh | [sequence-login.md](sequence-login.md) | [svg](img/sequence-login.svg) · [png](img/sequence-login.png) |
| Sequence: workflow → plan → approval → execute → review, and retry/failure | [sequence-workflow.md](sequence-workflow.md) | [1](img/sequence-workflow-1.svg) · [2](img/sequence-workflow-2.svg) |
| Sequence: deep research (sub-questions → parallel research → report → review) | [sequence-deep-research.md](sequence-deep-research.md) | [svg](img/sequence-deep-research.svg) · [png](img/sequence-deep-research.png) |
| Sequence: document ingestion + retrieval | [sequence-ingestion-retrieval.md](sequence-ingestion-retrieval.md) | [svg](img/sequence-ingestion-retrieval.svg) · [png](img/sequence-ingestion-retrieval.png) |
| Flow: run and step state machines | [flow-state-machine.md](flow-state-machine.md) | [1](img/flow-state-machine-1.svg) · [2](img/flow-state-machine-2.svg) |
| Flow: approval decision | [flow-approval.md](flow-approval.md) | [svg](img/flow-approval.svg) · [png](img/flow-approval.png) |
| Flow: retry, circuit breaker, tool gates | [flow-retry-circuit-breaker.md](flow-retry-circuit-breaker.md) | [1](img/flow-retry-circuit-breaker-1.svg) · [2](img/flow-retry-circuit-breaker-2.svg) |
| Flow: agent pipeline | [flow-agents.md](flow-agents.md) | [svg](img/flow-agents.svg) · [png](img/flow-agents.png) |
| Observability | [observability.md](observability.md) | [svg](img/observability.svg) · [png](img/observability.png) |
