"""Explicit state machines for runs and steps.

What: the ONLY place that says which status may follow which, plus ``assert_*_transition``
which raises ``InvalidTransitionError`` for anything else.
Why a state machine (not scattered ``if status == ...``): a workflow engine's worst bugs are
impossible states - a COMPLETED run that starts executing again, a step that "succeeds" after
being skipped. With one table, every status change is checked against it, the table can be
unit-tested exhaustively, and it doubles as the documentation diagram.
Notes on non-obvious edges:
 - run FAILED -> EXECUTING exists for one reason: an operator explicitly retrying a failed step.
 - run REVIEWING -> PLANNING is the revision loop (the Reviewer asked for a better plan).
 - step RUNNING -> PENDING is "wait and retry" (and crash recovery); RUNNING -> SKIPPED is cancel.
Connects to: ``store.py`` (applies transitions), ``engine.py``, tests/unit/test_state_machine.py.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.models.enums import RunStatus, StepStatus

R = RunStatus
S = StepStatus

RUN_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = {
    R.CREATED: frozenset({R.PLANNING, R.CANCELLED, R.FAILED}),
    R.PLANNING: frozenset({R.AWAITING_APPROVAL, R.EXECUTING, R.FAILED, R.CANCELLED}),
    R.AWAITING_APPROVAL: frozenset({R.EXECUTING, R.FAILED, R.CANCELLED}),
    R.EXECUTING: frozenset({R.REVIEWING, R.FAILED, R.CANCELLED}),
    R.REVIEWING: frozenset({R.COMPLETED, R.PLANNING, R.FAILED, R.CANCELLED}),
    R.COMPLETED: frozenset(),
    R.FAILED: frozenset({R.EXECUTING}),  # only via an explicit operator retry-step
    R.CANCELLED: frozenset(),
}

STEP_TRANSITIONS: Mapping[StepStatus, frozenset[StepStatus]] = {
    S.PENDING: frozenset({S.RUNNING, S.AWAITING_APPROVAL, S.SKIPPED}),
    S.AWAITING_APPROVAL: frozenset({S.PENDING, S.FAILED, S.SKIPPED}),
    S.RUNNING: frozenset({S.SUCCEEDED, S.FAILED, S.PENDING, S.AWAITING_APPROVAL, S.SKIPPED}),
    S.SUCCEEDED: frozenset(),
    S.FAILED: frozenset({S.PENDING}),  # operator retry
    S.SKIPPED: frozenset({S.PENDING}),  # dependents of a retried step run again
}

# Statuses in which nothing more will happen (FAILED can be re-opened only by a retry-step).
TERMINAL_RUN_STATUSES = frozenset({R.COMPLETED, R.FAILED, R.CANCELLED})
# A worker should be driving the run in these states.
ACTIVE_RUN_STATUSES = frozenset({R.CREATED, R.PLANNING, R.EXECUTING, R.REVIEWING})


class InvalidTransitionError(Exception):
    """A status change that the state machine does not allow."""

    def __init__(self, kind: str, current: str, target: str) -> None:
        super().__init__(f"invalid {kind} transition {current} -> {target}")
        self.kind = kind
        self.current = current
        self.target = target


def assert_run_transition(current: RunStatus, target: RunStatus) -> None:
    if target not in RUN_TRANSITIONS[current]:
        raise InvalidTransitionError("run", current.value, target.value)


def assert_step_transition(current: StepStatus, target: StepStatus) -> None:
    if target not in STEP_TRANSITIONS[current]:
        raise InvalidTransitionError("step", current.value, target.value)
