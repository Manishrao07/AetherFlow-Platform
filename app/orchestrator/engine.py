"""WorkflowEngine: drives one run from CREATED to a terminal state.

Pipeline: PLANNING (Planner agent) -> [AWAITING_APPROVAL] -> EXECUTING (DAG of steps) ->
REVIEWING (Review agent) -> COMPLETED, or back to PLANNING when the Reviewer asks for a revision.

Key design points (each is a likely interview question):
 * STATE LIVES IN POSTGRES, not in memory. ``drive`` is a loop that reads the run's status and
   does the work for that status, so it can be started on any run at any time - after an
   approval, after a restart, or on another machine after a crash. Nothing is "resumed" from a
   memory snapshot; the database row IS the checkpoint.
 * EXACTLY ONE DRIVER per run via a lease (see ``RunStore.claim``); a heartbeat renews it.
 * DAG SCHEDULING: a step is ready when all its dependencies SUCCEEDED. Ready steps run
   concurrently (bounded by a semaphore). A step whose dependency failed is SKIPPED.
 * RETRIES: only errors marked retryable (timeouts, 429/5xx, open breaker) are retried, with
   exponential backoff + full jitter; permanent errors (bad input, forbidden) fail at once.
 * IDEMPOTENCY: attempt N of a step always uses the same key, so a re-dispatched attempt replays
   the stored tool result instead of repeating a side effect.
 * FAILURES FEED THE REVIEW LOOP: a failed step does not abort the run immediately; the
   Reviewer sees the failure and can request a re-plan (bounded by ``max_revisions``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents import AgentSet
from app.agents.base import AgentContext, AgentOutputError
from app.agents.execution import ExecutionInput
from app.agents.plan import Plan
from app.agents.planner import PlannerInput
from app.agents.research import ResearchInput
from app.agents.retrieval import RetrievalInput
from app.agents.review import ReviewInput, StepResult
from app.agents.synthesizer import SynthesizerInput, confidence_for
from app.agents.templating import render_output
from app.core.config import Settings
from app.core.llm.base import CircuitOpenError, LLMError
from app.core.llm.client import LLMClient
from app.core.llm.retry import RetryPolicy, backoff_delay
from app.core.logging import bind_log_context
from app.core.observability.tracing import span
from app.core.rbac import Principal
from app.core.redaction import redact, redact_text
from app.memory.long_term import LongTermMemory
from app.memory.short_term import ShortTermMemory
from app.metrics import RUNS_IN_FLIGHT, STEP_RESULTS, STEP_RETRIES, WORKFLOW_REVISIONS
from app.models import StepDependency, User, WorkflowRun, WorkflowStep
from app.models.enums import AgentName, MessageRole, ReviewVerdict, RunStatus, StepStatus
from app.orchestrator.approvals import plan_is_approved, request_plan_approval
from app.orchestrator.store import RunInterrupted, RunStore, add_event
from app.services.conversations import ConversationService
from app.tools.base import ToolError, ToolValidationError

logger = logging.getLogger(__name__)

_FAILED_OR_SKIPPED = {StepStatus.FAILED, StepStatus.SKIPPED}


class WorkflowEngine:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        store: RunStore,
        agents: AgentSet,
        llm: LLMClient,
        owner: str,
    ) -> None:
        self._settings = settings
        self._sf = session_factory
        self._store = store
        self._agents = agents
        self._llm = llm
        self._owner = owner
        self._retry = RetryPolicy(
            max_attempts=settings.step_max_attempts,
            base_delay=settings.step_retry_base_delay_seconds,
            max_delay=max(
                settings.step_retry_max_delay_seconds, settings.step_retry_base_delay_seconds
            ),
        )

    # ================================================================ driver
    async def drive(self, run_id: uuid.UUID) -> bool:
        """Advance the run until it pauses (approval) or finishes. False if not ours to drive."""
        if not await self._store.claim(run_id, self._owner, self._settings.run_lease_seconds):
            return False
        bind_log_context(run_id=str(run_id))
        main = asyncio.current_task()
        lease_lost = False

        async def heartbeat() -> None:
            nonlocal lease_lost
            interval = self._settings.run_lease_seconds / 3
            while True:
                await asyncio.sleep(interval)
                if not await self._store.renew(
                    run_id, self._owner, self._settings.run_lease_seconds
                ):
                    lease_lost = True
                    logger.error("lost the lease on a run; stopping")
                    if main is not None:
                        main.cancel()
                    return

        beat = asyncio.create_task(heartbeat())
        RUNS_IN_FLIGHT.inc()
        try:
            # WHY trace id = run id: every span of a run, on any worker or after any restart,
            # lands in ONE trace that can be found from the run id alone.
            with span("workflow.run", trace_id=run_id.hex, run_id=str(run_id)):
                await self._loop(run_id)
        except RunInterrupted as stop:
            logger.info("run stopped", extra={"status": stop.status.value})
        except asyncio.CancelledError:
            if not lease_lost:
                raise
            return False
        except Exception as exc:
            logger.exception("run crashed")
            await self._fail_quietly(run_id, f"internal error: {type(exc).__name__}")
        finally:
            RUNS_IN_FLIGHT.dec()
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
            await asyncio.shield(self._store.release(run_id, self._owner))
        return True

    async def _loop(self, run_id: uuid.UUID) -> None:
        while True:
            run = await self._store.get_run(run_id)
            match run.status:
                case RunStatus.CREATED:
                    await self._store.transition_run(run_id, RunStatus.PLANNING)
                case RunStatus.PLANNING:
                    await self._plan(run)
                case RunStatus.EXECUTING:
                    await self._execute(run)
                case RunStatus.REVIEWING:
                    await self._review(run)
                case _:  # AWAITING_APPROVAL or terminal: nothing for a worker to do
                    return

    async def _fail(self, run_id: uuid.UUID, message: str) -> None:
        await self._store.transition_run(
            run_id, RunStatus.FAILED, reason=message[:200], error=redact_text(message)[:2000]
        )

    async def _fail_quietly(self, run_id: uuid.UUID, message: str) -> None:
        with contextlib.suppress(Exception):
            await self._fail(run_id, message)

    async def _principal(self, user_id: uuid.UUID) -> Principal | None:
        """Rebuild the caller from the database NOW (roles may have changed since submit)."""
        async with self._sf() as session:
            user = await session.get(User, user_id)
        if user is None or not user.is_active:
            return None
        return Principal(user.id, user.email, frozenset(r.name for r in user.roles))

    # ============================================================== planning
    async def _plan(self, run: WorkflowRun) -> None:
        principal = await self._principal(run.user_id)
        if principal is None:
            await self._fail(run.id, "the user who started this run is inactive or deleted")
            return
        feedback = (run.result or {}).get("review_feedback")
        previous = Plan.model_validate(run.plan) if run.plan and feedback else None
        payload = PlannerInput(
            goal=run.goal,
            memories=await self._memories(run),
            feedback=feedback,
            previous_plan=previous,
            context=await self._conversation_context(run),
            mode=run.mode,
        )
        try:
            with span("agent.planner", revision=run.revision_count):
                plan = await self._agents.planner.run(payload, AgentContext(principal, run.id))
        except (AgentOutputError, LLMError) as exc:
            await self._fail(run.id, f"planning failed: {exc}")
            return

        needs_approval = plan.needs_approval

        async def persist(session: AsyncSession, current: WorkflowRun) -> None:
            await self._insert_steps(session, current, plan)
            if needs_approval:
                await request_plan_approval(
                    session, current, plan, self._settings.approval_ttl_seconds
                )

        await self._store.transition_run(
            run.id,
            RunStatus.AWAITING_APPROVAL if needs_approval else RunStatus.EXECUTING,
            reason="plan needs human approval" if needs_approval else "plan ready",
            extra=persist,
            plan=plan.model_dump(mode="json"),
            error=None,
        )

    async def _insert_steps(self, session: AsyncSession, run: WorkflowRun, plan: Plan) -> None:
        rows: dict[str, WorkflowStep] = {}
        for position, step in enumerate(plan.steps):
            rows[step.key] = WorkflowStep(
                run_id=run.id,
                step_key=step.key,
                revision=run.revision_count,
                position=position,
                agent=step.agent,
                tool=step.tool,
                inputs=step.inputs,
                risk_level=step.risk_level,
                requires_approval=step.requires_approval,
                max_attempts=self._settings.step_max_attempts,
            )
        session.add_all(rows.values())
        await session.flush()
        session.add_all(
            StepDependency(step_id=rows[s.key].id, depends_on_step_id=rows[d].id)
            for s in plan.steps
            for d in s.depends_on
        )
        add_event(
            session, run.id, "plan.created",
            {"revision": run.revision_count, "summary": plan.summary,
             "steps": [s.key for s in plan.steps], "layers": plan.layers(),
             "needs_approval": plan.needs_approval, "max_risk": plan.max_risk.value},
        )  # fmt: skip

    async def _memories(self, run: WorkflowRun) -> list[str]:
        async with self._sf() as session, session.begin():
            found = await LongTermMemory(session).search(run.user_id, run.goal, top_k=5)
        return [f"{m.key}: {m.content}" for m in found]

    async def _conversation_context(self, run: WorkflowRun) -> str | None:
        if run.conversation_id is None:
            return None
        s = self._settings
        async with self._sf() as session, session.begin():
            conversation = await ConversationService(session).get(run.user_id, run.conversation_id)
            window = await ShortTermMemory(
                session,
                self._llm,
                token_budget=s.short_term_token_budget,
                keep_recent=s.short_term_keep_recent,
                summary_max_tokens=s.summary_max_tokens,
            ).build(conversation)
        lines = [f"Summary of earlier messages: {window.summary}"] if window.summary else []
        lines += [f"{m.role}: {m.content}" for m in window.messages]
        return "\n".join(lines) or None

    # ============================================================== execution
    async def _execute(self, run: WorkflowRun) -> None:
        principal = await self._principal(run.user_id)
        if principal is None:
            await self._fail(run.id, "the user who started this run is inactive or deleted")
            return
        plan = Plan.model_validate(run.plan)
        deps = {s.key: s.depends_on for s in plan.steps}
        approved = await plan_is_approved(self._sf, run.id)
        gate = asyncio.Semaphore(self._settings.max_parallel_steps)
        running: dict[str, asyncio.Task[None]] = {}
        try:
            while True:
                await self._ensure_status(run.id, RunStatus.EXECUTING)
                steps = {
                    s.step_key: s for s in await self._store.get_steps(run.id, run.revision_count)
                }
                skipped = await self._skip_blocked(steps, deps, running)
                ready = [
                    s for k, s in steps.items()
                    if s.status is StepStatus.PENDING and k not in running
                    and all(steps[d].status is StepStatus.SUCCEEDED for d in deps[k])
                ]  # fmt: skip
                for step in ready:
                    upstream = {d: steps[d].output or {} for d in deps[step.step_key]}
                    running[step.step_key] = asyncio.create_task(
                        self._run_step(run, step, plan, principal, upstream, approved, gate)
                    )
                if not running:
                    if skipped:
                        continue  # a skip may unblock further skips; re-evaluate
                    break
                done, _ = await asyncio.wait(running.values(), return_when=asyncio.FIRST_COMPLETED)
                for key in [k for k, t in running.items() if t in done]:
                    task = running.pop(key)
                    task.result()  # re-raise RunInterrupted / unexpected errors
        except BaseException:
            await self._abandon(run, running)
            raise
        await self._store.transition_run(run.id, RunStatus.REVIEWING, reason="all steps settled")

    async def _skip_blocked(
        self,
        steps: dict[str, WorkflowStep],
        deps: dict[str, list[str]],
        running: dict[str, asyncio.Task[None]],
    ) -> bool:
        """SKIP pending steps that depend on a failed/skipped step. True if any changed."""
        changed = False
        for key, step in steps.items():
            if step.status is not StepStatus.PENDING or key in running:
                continue
            blocker = next((d for d in deps[key] if steps[d].status in _FAILED_OR_SKIPPED), None)
            if blocker:
                await self._store.transition_step(
                    step.id, StepStatus.SKIPPED, reason=f"dependency '{blocker}' did not succeed"
                )
                STEP_RESULTS.labels(step.agent.value, step.tool or "none", "skipped").inc()
                changed = True
        return changed

    async def _abandon(self, run: WorkflowRun, running: dict[str, asyncio.Task[None]]) -> None:
        """Stop in-flight step tasks; mark whatever never finished as SKIPPED."""
        for task in running.values():
            task.cancel()
        await asyncio.gather(*running.values(), return_exceptions=True)
        current = await self._store.get_run(run.id)
        if current.status in {RunStatus.CANCELLED, RunStatus.FAILED}:
            for step in await self._store.get_steps(run.id, run.revision_count):
                if step.status in {StepStatus.PENDING, StepStatus.RUNNING}:
                    with contextlib.suppress(Exception):
                        await self._store.transition_step(
                            step.id, StepStatus.SKIPPED, reason=f"run {current.status.value}"
                        )

    async def _ensure_status(self, run_id: uuid.UUID, expected: RunStatus) -> None:
        current = await self._store.get_run(run_id)
        if current.status is not expected:
            raise RunInterrupted(current.status)

    async def _run_step(
        self,
        run: WorkflowRun,
        step: WorkflowStep,
        plan: Plan,
        principal: Principal,
        upstream: dict[str, dict[str, Any]],
        plan_approved: bool,
        gate: asyncio.Semaphore,
    ) -> None:
        """Run one step with retries. Never raises for step failures - it records them."""
        async with gate:
            bind_log_context(run_id=str(run.id), step_id=str(step.id))
            allowed = step.requires_approval and plan_approved
            while True:
                await self._ensure_status(run.id, RunStatus.EXECUTING)
                attempt = step.attempts + 1
                step = await self._store.transition_step(
                    step.id, StepStatus.RUNNING, attempts=attempt, error=None
                )
                try:
                    with span("step.run", step=step.step_key, attempt=attempt):
                        output = await self._invoke(run, step, plan, principal, upstream, allowed)
                except RunInterrupted:
                    raise
                except Exception as exc:
                    retryable, message = classify_error(exc)
                    if retryable and attempt < step.max_attempts:
                        delay = backoff_delay(
                            attempt, self._retry, retry_after=getattr(exc, "retry_after", None)
                        )
                        STEP_RETRIES.labels(step.tool or "none").inc()
                        step = await self._store.transition_step(
                            step.id, StepStatus.PENDING, error=message, reason="will retry"
                        )
                        await self._store.emit(
                            run.id, "step.retry",
                            {"key": step.step_key, "attempt": attempt, "delay_s": round(delay, 3),
                             "error": message},
                        )  # fmt: skip
                        await asyncio.sleep(delay)
                        continue
                    await self._store.transition_step(
                        step.id, StepStatus.FAILED, error=message,
                        reason="retries exhausted" if retryable else "non-retryable error",
                    )  # fmt: skip
                    STEP_RESULTS.labels(step.agent.value, step.tool or "none", "failed").inc()
                    return
                await self._store.transition_step(
                    step.id, StepStatus.SUCCEEDED, output=redact(output), error=None
                )
                STEP_RESULTS.labels(step.agent.value, step.tool or "none", "succeeded").inc()
                return

    async def _invoke(
        self,
        run: WorkflowRun,
        step: WorkflowStep,
        plan: Plan,
        principal: Principal,
        upstream: dict[str, dict[str, Any]],
        approved: bool,
    ) -> dict[str, Any]:
        ctx = AgentContext(principal, run.id, step.id)
        if step.agent is AgentName.RETRIEVAL:
            with span("agent.retrieval", step=step.step_key):
                found = await self._agents.retrieval.run(
                    RetrievalInput(
                        query=str(step.inputs["query"]),
                        top_k=int(step.inputs.get("top_k", self._settings.retrieval_top_k)),
                        attempt=step.attempts,
                        plan_tools=plan.tools,
                        approved=approved,
                    ),
                    ctx,
                )
            return found.model_dump(mode="json")
        if step.agent is AgentName.RESEARCH:
            with span("agent.research", step=step.step_key):
                researched = await self._agents.research.run(
                    ResearchInput(
                        question=str(step.inputs["question"]),
                        sources=[str(t) for t in step.inputs["sources"]],
                        max_results=self._settings.research_max_results,
                        attempt=step.attempts,
                        plan_tools=plan.tools,
                        approved=approved,
                    ),
                    ctx,
                )
            return researched.model_dump(mode="json")
        if step.agent is AgentName.SYNTHESIZER:
            with span("agent.synthesizer", step=step.step_key):
                written = await self._agents.synthesizer.run(
                    SynthesizerInput(topic=str(step.inputs["topic"]), upstream=upstream), ctx
                )
            return written.model_dump(mode="json")
        assert step.tool is not None  # guaranteed by plan validation
        with span("agent.execution", step=step.step_key, tool=step.tool):
            done = await self._agents.execution.run(
                ExecutionInput(
                    tool=step.tool,
                    inputs=step.inputs,
                    attempt=step.attempts,
                    plan_tools=plan.tools,
                    upstream=upstream,
                    approved=approved,
                ),
                ctx,
            )
        return done.model_dump(mode="json")

    # ================================================================ review
    async def _review(self, run: WorkflowRun) -> None:
        principal = await self._principal(run.user_id)
        if principal is None:
            await self._fail(run.id, "the user who started this run is inactive or deleted")
            return
        plan = Plan.model_validate(run.plan)
        steps = await self._store.get_steps(run.id, run.revision_count)
        results = [
            StepResult(
                key=s.step_key, agent=s.agent, tool=s.tool, status=s.status,
                output=s.output, error=s.error,
            )
            for s in steps
        ]  # fmt: skip
        previous_feedback = (run.result or {}).get("review_feedback")
        try:
            with span("agent.review", revision=run.revision_count):
                verdict = await self._agents.review.run(
                    ReviewInput(
                        goal=run.goal,
                        plan=plan,
                        results=results,
                        revision=run.revision_count,
                        previous_feedback=previous_feedback,
                    ),
                    AgentContext(principal, run.id),
                )
        except (AgentOutputError, LLMError) as exc:
            await self._fail(run.id, f"review failed: {exc}")
            return

        await self._store.emit(
            run.id, "review.verdict",
            {"verdict": verdict.verdict.value, "score": verdict.score, "reasons": verdict.reasons,
             "revision": run.revision_count},
        )  # fmt: skip
        review_fields: dict[str, Any] = {
            "review_verdict": verdict.verdict,
            "review_score": round(verdict.score, 3),
        }
        if verdict.verdict is ReviewVerdict.APPROVED:
            summary = summarise_results(plan, steps)
            result: dict[str, Any] = {
                "summary": summary,
                "steps": {s.step_key: s.output for s in steps},
                "review": {"verdict": verdict.verdict.value, "score": verdict.score,
                           "reasons": verdict.reasons},
            }  # fmt: skip
            if confidence := confidence_for([s.output for s in steps if s.output], verdict.score):
                result["confidence"] = confidence
            await self._store.transition_run(
                run.id, RunStatus.COMPLETED, reason="review approved", result=result,
                error=None, **review_fields,
            )  # fmt: skip
            await self._post_reply(run, summary)
        elif (
            verdict.verdict is ReviewVerdict.NEEDS_REVISION
            and run.revision_count < run.max_revisions
        ):
            WORKFLOW_REVISIONS.inc()
            await self._store.transition_run(
                run.id, RunStatus.PLANNING,
                reason=f"revision {run.revision_count + 1} requested by reviewer",
                revision_count=run.revision_count + 1,
                result={"review_feedback": verdict.feedback, "reasons": verdict.reasons},
                **review_fields,
            )  # fmt: skip
        else:
            await self._store.transition_run(
                run.id, RunStatus.FAILED, reason=f"review {verdict.verdict.value}",
                error=f"Review {verdict.verdict.value}: " + "; ".join(verdict.reasons)[:1800],
                **review_fields,
            )  # fmt: skip

    async def _post_reply(self, run: WorkflowRun, text: str) -> None:
        """Add the run's outcome to its conversation as an assistant message (best effort)."""
        if run.conversation_id is None:
            return
        try:
            async with self._sf() as session, session.begin():
                await ConversationService(session).append(
                    run.user_id, run.conversation_id, text, MessageRole.ASSISTANT, run.id
                )
        except Exception:
            logger.warning("could not post the run result to its conversation", exc_info=True)

    # ============================================================== recovery
    async def recover(self) -> list[uuid.UUID]:
        """Find runs whose worker died, repair their interrupted steps, return them to drive."""
        orphans = await self._store.find_orphans()
        for run_id in orphans:
            repaired = await self._store.reset_interrupted_steps(run_id)
            if repaired:
                logger.warning("recovered interrupted steps",
                               extra={"run_id": str(run_id), "steps": repaired})  # fmt: skip
        return orphans


def classify_error(exc: BaseException) -> tuple[bool, str]:
    """Decide whether a step error is worth retrying; return a safe, redacted message."""
    retryable: bool
    if isinstance(exc, ToolValidationError):
        retryable, text = False, f"{exc}: {exc.details}"
    elif isinstance(exc, ToolError):
        retryable, text = exc.retryable, str(exc)
    elif isinstance(exc, CircuitOpenError):
        retryable, text = True, f"dependency temporarily unavailable: {exc}"  # breaker will recover
    elif isinstance(exc, LLMError):
        retryable, text = exc.retryable, f"LLM error: {exc}"
    elif isinstance(exc, AgentOutputError):
        retryable, text = True, str(exc)  # a fresh model answer may well be valid
    else:
        retryable, text = False, f"{type(exc).__name__}: {exc}"
    return retryable, redact_text(text)[:500]


_NOISE_KEYS = frozenset({"duration_ms", "replayed", "tool", "truncated"})


def _describe_output(output: dict[str, Any]) -> str:
    """Readable text for one step output: cited snippets, an answer, or ``key: value`` pairs."""
    inner = output.get("output")
    if isinstance(inner, dict):
        # WHY: tool results are wrapped with timing/replay bookkeeping; users want the payload.
        output = inner
    if isinstance(output.get("results"), list) or isinstance(output.get("answer"), str):
        return render_output(output)
    pairs = [f"{k}: {v}" for k, v in sorted(output.items()) if k not in _NOISE_KEYS]
    return ", ".join(pairs) if pairs else render_output(output)


def summarise_results(plan: Plan, steps: list[WorkflowStep]) -> str:
    """Human-readable outcome used as the run summary and the conversation reply."""
    by_key = {s.step_key: s for s in steps}
    # WHY: a deep-research run's answer IS the report; per-step one-liners would only truncate it.
    for step in steps:
        if step.output and isinstance(step.output.get("report"), str):
            return str(step.output["report"])
    lines = [plan.summary]
    for planned in plan.steps:
        step = by_key[planned.key]
        text = _describe_output(step.output) if step.output else (step.error or "-")
        lines.append(f"- {planned.description}: {text[:400]}")
    return "\n\n".join(lines)  # WHY: blank lines so each step is its own paragraph in the UI


__all__ = ["WorkflowEngine", "classify_error", "summarise_results"]
