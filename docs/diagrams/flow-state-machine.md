# Flowchart: run and step state machines

Source of truth: `app/orchestrator/state_machine.py`. Any other transition raises
`InvalidTransitionError`, and every legal one is written to `run_events` in the same transaction.

## Run

```mermaid
stateDiagram-v2
    [*] --> CREATED
    CREATED --> PLANNING
    PLANNING --> AWAITING_APPROVAL: plan has a HIGH-risk step
    PLANNING --> EXECUTING: no approval needed
    AWAITING_APPROVAL --> EXECUTING: approved
    AWAITING_APPROVAL --> CANCELLED: rejected / cancelled
    AWAITING_APPROVAL --> FAILED: approval expired
    EXECUTING --> REVIEWING: all steps finished
    REVIEWING --> COMPLETED: APPROVED
    REVIEWING --> PLANNING: NEEDS_REVISION (revisions left)
    REVIEWING --> FAILED: REJECTED / no revisions left
    FAILED --> EXECUTING: operator retries a step
    CREATED --> FAILED
    PLANNING --> FAILED: plan invalid / LLM down
    EXECUTING --> FAILED
    CREATED --> CANCELLED
    PLANNING --> CANCELLED
    EXECUTING --> CANCELLED
    REVIEWING --> CANCELLED
    COMPLETED --> [*]
    CANCELLED --> [*]
```

## Step

```mermaid
stateDiagram-v2
    [*] --> PENDING
    PENDING --> RUNNING: dependencies done
    PENDING --> SKIPPED: a dependency failed / run ended
    RUNNING --> SUCCEEDED
    RUNNING --> FAILED: attempts used up or non-retryable error
    RUNNING --> PENDING: worker crashed (recovery) / retry
    FAILED --> PENDING: operator retry-step
    SKIPPED --> PENDING: upstream step retried
    SUCCEEDED --> [*]
```
