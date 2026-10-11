"""Workflow-platform metrics: runs, steps, tools and approvals (Project 1 specific).

What: Prometheus counters/histograms registered on the shared registry from
``app/core/observability/metrics.py``.
Why separate from ``core``: ``core`` stays generic (HTTP, LLM, cache) and copyable to another
project; these names describe THIS product's domain.
Label policy: only small fixed sets (status names, the 7 tool names, 4 agent names). Run ids, user
ids and error texts are never labels - they belong in logs and the run timeline.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

from app.core.observability.metrics import REGISTRY

_RUN_BUCKETS = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 900)
_TOOL_BUCKETS = (0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 15)
# Approvals wait for humans: seconds up to a day.
_APPROVAL_BUCKETS = (1, 5, 15, 60, 300, 900, 3600, 4 * 3600, 12 * 3600, 86400)

WORKFLOW_RUNS = Counter(
    "workflow_runs_total",
    "Runs that reached a final state (a retried run can finish more than once)",
    ["status"],
    registry=REGISTRY,
)
WORKFLOW_DURATION = Histogram(
    "workflow_duration_seconds",
    "Time from creation to a final state",
    ["status"],
    buckets=_RUN_BUCKETS,
    registry=REGISTRY,
)
WORKFLOWS_CREATED = Counter("workflows_created_total", "Runs created", registry=REGISTRY)
WORKFLOW_REVISIONS = Counter(
    "workflow_revisions_total", "Review-driven re-plans", registry=REGISTRY
)
RUNS_IN_FLIGHT = Gauge("workflow_runs_in_flight", "Runs this worker is driving", registry=REGISTRY)

STEP_RESULTS = Counter(
    "workflow_steps_total",
    "Steps that ended (status: succeeded | failed | skipped)",
    ["agent", "tool", "status"],
    registry=REGISTRY,
)
STEP_RETRIES = Counter(
    "workflow_step_retries_total", "Step attempts that were retried", ["tool"], registry=REGISTRY
)

TOOL_EXECUTIONS = Counter(
    "tool_executions_total",
    "Tool calls (status: succeeded | failed | timeout | blocked | replayed)",
    ["tool", "status"],
    registry=REGISTRY,
)
TOOL_DURATION = Histogram(
    "tool_duration_seconds", "Tool run time", ["tool"], buckets=_TOOL_BUCKETS, registry=REGISTRY
)

APPROVAL_DECISIONS = Counter(
    "approval_decisions_total",
    "Approval outcomes (approved | rejected | expired)",
    ["decision"],
    registry=REGISTRY,
)
APPROVAL_WAIT = Histogram(
    "approval_wait_seconds",
    "Time an approval waited for a human",
    ["decision"],
    buckets=_APPROVAL_BUCKETS,
    registry=REGISTRY,
)
