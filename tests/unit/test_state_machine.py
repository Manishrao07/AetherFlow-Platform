"""The run/step state machines: the full allowed set, exhaustively."""

import itertools

import pytest

from app.models.enums import RunStatus, StepStatus
from app.orchestrator.state_machine import (
    ACTIVE_RUN_STATUSES,
    RUN_TRANSITIONS,
    STEP_TRANSITIONS,
    TERMINAL_RUN_STATUSES,
    InvalidTransitionError,
    assert_run_transition,
    assert_step_transition,
)

R, S = RunStatus, StepStatus

ALLOWED_RUN = {
    (R.CREATED, R.PLANNING), (R.CREATED, R.CANCELLED), (R.CREATED, R.FAILED),
    (R.PLANNING, R.AWAITING_APPROVAL), (R.PLANNING, R.EXECUTING), (R.PLANNING, R.FAILED),
    (R.PLANNING, R.CANCELLED),
    (R.AWAITING_APPROVAL, R.EXECUTING), (R.AWAITING_APPROVAL, R.FAILED),
    (R.AWAITING_APPROVAL, R.CANCELLED),
    (R.EXECUTING, R.REVIEWING), (R.EXECUTING, R.FAILED), (R.EXECUTING, R.CANCELLED),
    (R.REVIEWING, R.COMPLETED), (R.REVIEWING, R.PLANNING), (R.REVIEWING, R.FAILED),
    (R.REVIEWING, R.CANCELLED),
    (R.FAILED, R.EXECUTING),
}  # fmt: skip

ALLOWED_STEP = {
    (S.PENDING, S.RUNNING), (S.PENDING, S.AWAITING_APPROVAL), (S.PENDING, S.SKIPPED),
    (S.AWAITING_APPROVAL, S.PENDING), (S.AWAITING_APPROVAL, S.FAILED),
    (S.AWAITING_APPROVAL, S.SKIPPED),
    (S.RUNNING, S.SUCCEEDED), (S.RUNNING, S.FAILED), (S.RUNNING, S.PENDING),
    (S.RUNNING, S.AWAITING_APPROVAL), (S.RUNNING, S.SKIPPED),
    (S.FAILED, S.PENDING), (S.SKIPPED, S.PENDING),
}  # fmt: skip


@pytest.mark.parametrize(("current", "target"), list(itertools.product(R, R)))
def test_every_run_transition(current: RunStatus, target: RunStatus) -> None:
    if (current, target) in ALLOWED_RUN:
        assert_run_transition(current, target)
    else:
        with pytest.raises(InvalidTransitionError, match=f"{current.value} -> {target.value}"):
            assert_run_transition(current, target)


@pytest.mark.parametrize(("current", "target"), list(itertools.product(S, S)))
def test_every_step_transition(current: StepStatus, target: StepStatus) -> None:
    if (current, target) in ALLOWED_STEP:
        assert_step_transition(current, target)
    else:
        with pytest.raises(InvalidTransitionError):
            assert_step_transition(current, target)


def test_tables_cover_every_status_and_match_the_spec() -> None:
    assert set(RUN_TRANSITIONS) == set(R) and set(STEP_TRANSITIONS) == set(S)
    assert {(a, b) for a, targets in RUN_TRANSITIONS.items() for b in targets} == ALLOWED_RUN


def test_terminal_and_active_sets() -> None:
    assert {R.COMPLETED, R.FAILED, R.CANCELLED} == TERMINAL_RUN_STATUSES
    assert {R.CREATED, R.PLANNING, R.EXECUTING, R.REVIEWING} == ACTIVE_RUN_STATUSES
    assert not (TERMINAL_RUN_STATUSES & ACTIVE_RUN_STATUSES)
    # a finished run can never restart, except FAILED via an explicit operator retry-step
    assert (
        RUN_TRANSITIONS[R.COMPLETED] == frozenset() and RUN_TRANSITIONS[R.CANCELLED] == frozenset()
    )
    assert RUN_TRANSITIONS[R.FAILED] == {R.EXECUTING}
    assert STEP_TRANSITIONS[S.SUCCEEDED] == frozenset()


def test_error_carries_details() -> None:
    with pytest.raises(InvalidTransitionError) as info:
        assert_run_transition(R.COMPLETED, R.EXECUTING)
    assert (info.value.kind, info.value.current, info.value.target) == (
        "run",
        "COMPLETED",
        "EXECUTING",
    )
