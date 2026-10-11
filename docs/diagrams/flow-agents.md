# Flowchart: how the four agents fit together

The orchestrator (Phase 5) drives this pipeline; each box is an agent or a check in code.

```mermaid
flowchart TD
    G[User goal + long-term memories] --> P[PlannerAgent: LLM returns JSON plan]
    P --> V{Valid?<br/>schema, no cycles,<br/>tools exist, user may use them,<br/>inputs match tool schema}
    V -- no, first time --> R[Repair prompt shows the error] --> P
    V -- no, again --> F[Run FAILED: planner output invalid]
    V -- yes --> N[Overwrite risk and approval flags<br/>from each tool's own metadata]
    N --> A{Plan needs<br/>approval?}
    A -- yes --> H[Human approves / rejects]
    A -- no --> D
    H -- approved --> D[Schedule DAG: independent steps run in parallel]
    H -- rejected --> X[Run CANCELLED]
    D --> RS[RetrievalAgent steps:<br/>search, cited answer]
    D --> ES[ExecutionAgent steps:<br/>tool via registry gates]
    RS --> RV[ReviewAgent]
    ES --> RV
    RV --> E{Policy check in code:<br/>all steps succeeded<br/>and score >= minimum?}
    E -- APPROVED --> C[Run COMPLETED]
    E -- NEEDS_REVISION --> M{Revisions left?}
    M -- yes --> P
    M -- no --> F2[Run FAILED]
    E -- REJECTED --> F2
```

| Agent | Tools it may call | Why |
|---|---|---|
| Planner | none | planning must have no side effects |
| Retrieval | `search_knowledge_base` | read-only |
| Execution | the seven built-in tools, **only those in the approved plan** | the plan, not the model, decides |
| Review | none | an independent reviewer cannot act |
