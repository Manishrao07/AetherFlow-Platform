# Sequence: document ingestion and retrieval

```mermaid
sequenceDiagram
    autonumber
    actor Op as Operator
    participant API as FastAPI /documents
    participant Svc as DocumentService
    participant DB as PostgreSQL
    participant Ret as RetrievalAgent
    participant Exec as ToolExecutor + Registry
    participant LLM as LLMClient (breaker, retry)

    Note over Op,DB: Ingestion (idempotent)
    Op->>API: POST /documents {title, content}
    API->>API: RBAC: document:write, body size limit
    API->>Svc: ingest(principal, title, content)
    Svc->>Svc: clean text (drop NUL, normalise newlines)
    Svc->>Svc: chunk (paragraph-aware, overlap) + sha256
    Svc->>DB: SELECT by (owner, sha256)
    alt identical content already stored
        Svc-->>API: existing document, created=false
        API-->>Op: 200 OK
    else new content
        Svc->>DB: INSERT document + chunks (tsvector is a generated column)
        Svc->>DB: COMMIT, then audit "document.ingested"
        API-->>Op: 201 Created
    end

    Note over Ret,LLM: Retrieval inside a workflow step
    Ret->>Exec: execute(search_knowledge_base, {query, top_k})
    Exec->>Exec: gates: in plan, in agent allow-list, role, schema
    Exec->>DB: to_tsquery (OR terms) + ts_rank_cd over GIN index
    DB-->>Exec: top-k chunks
    Exec->>DB: store tool_executions row (redacted)
    Exec-->>Ret: passages with citations
    alt no passages
        Ret-->>Ret: answer "nothing found", grounded=false (no LLM call)
    else passages found
        Ret->>LLM: question + passages fenced as untrusted data, cited by id (P1, P2)
        LLM-->>Ret: {answer, citations:[ids]}
        Ret->>Ret: keep only ids that were really retrieved, map to "Title #n"
        Ret-->>Ret: grounded = at least one valid citation
    end
```

**Why these choices**

- Ingestion de-duplicates on a content hash, enforced by a unique index, so a retried upload
  cannot crowd search results with duplicate chunks.
- Search runs through the tool layer, not directly: the same gates, audit and timeline apply to
  retrieval as to every other action.
- Passages are untrusted: they are fenced, the model is told never to follow instructions inside
  them, and its citations are verified against what was actually retrieved.
