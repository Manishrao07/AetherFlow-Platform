"""Tool abstractions: the Tool base class, execution context and error types.

What: every capability an agent can use (calculator, ticket creation, ...) is a ``Tool`` with
a typed input model, a risk level, a required permission and a timeout.
Why declare all that on the tool: safety properties live NEXT TO the code they protect, and
the registry enforces them uniformly - an individual tool cannot "forget" a check.
Connects to: ``registry.py`` (enforcement), ``executor.py`` (persistence), builtin tools.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.rbac import Principal
from app.models.enums import RiskLevel


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool may need about the current call (never model-controlled)."""

    principal: Principal
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    run_id: uuid.UUID | None = None
    step_id: uuid.UUID | None = None
    # Unique per ATTEMPT ("<step>:<attempt>"): lets the executor replay a finished attempt.
    idempotency_key: str = ""
    # Unique per STEP: tools that create side effects dedupe on this so that a retry of the
    # same step can never create a second ticket / send a second email.
    side_effect_key: str = ""
    # True only after a human approved this step (set by the orchestrator, never by a model).
    approved: bool = False


class ToolError(Exception):
    """A tool failed. ``retryable`` tells the orchestrator whether trying again can help."""

    retryable = False

    def __init__(self, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        if retryable is not None:
            self.retryable = retryable


class ToolTimeoutError(ToolError):
    retryable = True


class ToolNotFoundError(ToolError):
    pass


class ToolNotAllowedError(ToolError):
    """Blocked by the plan allow-list, the agent allow-list, or the user's permissions."""


class ToolValidationError(ToolError):
    def __init__(self, message: str, details: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.details = details or []


class ApprovalRequiredError(ToolError):
    """The tool needs human approval before it may run (orchestrator pauses the step)."""


class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[type[BaseModel]]
    risk: ClassVar[RiskLevel] = RiskLevel.LOW
    permission: ClassVar[str]
    timeout_seconds: ClassVar[float] = 15.0
    # External-facing tools get a circuit breaker so one dead dependency fails fast.
    uses_breaker: ClassVar[bool] = False

    @property
    def requires_approval(self) -> bool:
        """HIGH-risk tools always need a human in the loop."""
        return self.risk is RiskLevel.HIGH

    @abstractmethod
    async def run(self, args: Any, ctx: ToolContext) -> dict[str, Any]:
        """Do the work. ``args`` is an already-validated instance of ``input_model``."""

    def spec(self) -> dict[str, Any]:
        """Public description (used by the API and, later, by the planner prompt)."""
        return {
            "name": self.name,
            "description": self.description,
            "risk_level": self.risk.value,
            "requires_approval": self.requires_approval,
            "permission": self.permission,
            "timeout_seconds": self.timeout_seconds,
            "input_schema": self.input_model.model_json_schema(),
        }
