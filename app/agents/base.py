"""Shared agent machinery: the Agent base class, context objects and JSON-from-LLM parsing.

What: every agent is a class with a typed input -> typed output contract, its own system
prompt and its own tool allow-list. ``LLMAgent.complete_json`` asks the model for JSON,
validates it against a Pydantic model and, if invalid, makes ONE repair attempt that shows
the model its own error.
Why a repair attempt but no more: models usually fix a schema slip when told exactly what was
wrong, and a hard cap (one) keeps cost and latency predictable.
Why tagged prompts (``tagged``): user text and retrieved text go inside XML-style tags so the
model can tell data from instructions; closing tags inside the data are stripped so the data
cannot "escape" its fence (prompt-injection defence, OWASP LLM01).
Connects to: planner / retrieval / execution / review agents, ``mock_handlers.py``.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar, Generic, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.llm.base import LLMMessage, LLMRequest
from app.core.llm.client import CallContext, LLMClient
from app.core.rbac import Principal
from app.models.enums import AgentName
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

InT = TypeVar("InT")
OutT = TypeVar("OutT")
ModelT = TypeVar("ModelT", bound=BaseModel)


class AgentOutputError(Exception):
    """The model's answer could not be turned into a valid, policy-compliant output."""


@dataclass(frozen=True)
class AgentDeps:
    """Long-lived collaborators shared by all agents (built once at startup)."""

    llm: LLMClient
    registry: ToolRegistry
    executor: ToolExecutor
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings


@dataclass(frozen=True)
class AgentContext:
    """Per-call facts: who is asking and which run/step this work belongs to."""

    principal: Principal
    run_id: uuid.UUID | None = None
    step_id: uuid.UUID | None = None


class Agent(ABC, Generic[InT, OutT]):
    """Typed input -> typed output. Subclasses declare a name, prompt and tool allow-list."""

    name: ClassVar[AgentName]
    system_prompt: ClassVar[str] = ""
    # WHY frozenset on the class: least privilege is visible at a glance, and the registry
    # enforces it on every tool call (gate 3).
    allowed_tools: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps

    @abstractmethod
    async def run(self, payload: InT, ctx: AgentContext) -> OutT:
        """Do this agent's job."""


class LLMAgent(Agent[InT, OutT], ABC):
    """An agent whose work involves asking the LLM for structured JSON."""

    async def complete_json(
        self,
        *,
        task: str,
        user_message: str,
        model: type[ModelT],
        ctx: AgentContext,
        max_tokens: int = 1500,
        system: str | None = None,
        check: Callable[[ModelT], ModelT] | None = None,
    ) -> ModelT:
        """Ask for JSON matching ``model``; validate; repair once; else ``AgentOutputError``.

        ``system`` overrides the class prompt (the Planner builds it per user).
        ``check`` is an extra semantic validation (e.g. "tool exists"); raise ``ValueError``
        from it to trigger the same repair attempt as a schema error.
        """
        call = CallContext(self.name.value, ctx.run_id, ctx.step_id)
        messages = [LLMMessage("user", user_message)]
        last_error = ""
        for attempt in (1, 2):
            request = LLMRequest(
                system=system or self.system_prompt,
                messages=messages,
                task=task,
                max_tokens=max_tokens,
            )
            response = await self.deps.llm.complete(request, call)
            try:
                parsed = parse_json_model(response.text, model)
                return check(parsed) if check else parsed
            except (ValueError, ValidationError) as exc:
                # WHY: an answer that failed validation must not be served again from the cache.
                await self.deps.llm.evict(request)
                last_error = _short(exc)
                logger.warning(
                    "agent output invalid",
                    extra={"agent": self.name.value, "task": task, "attempt": attempt},
                )
                messages = [
                    *messages,
                    LLMMessage("assistant", response.text[:2000]),
                    LLMMessage(
                        "user",
                        "Your previous answer was invalid: "
                        f"{last_error}\nReturn ONLY the corrected JSON object.",
                    ),
                ]
        raise AgentOutputError(f"{self.name.value} produced invalid output: {last_error}")


def _short(exc: Exception) -> str:
    """One-line description of a validation problem (no echoed input values)."""
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'root'}: {e['msg']}" for e in exc.errors()
        )[:600]
    return str(exc)[:600]


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json_object(text: str) -> str:
    """Pull the first JSON object out of model text (tolerates code fences and chatter)."""
    fenced = _FENCE.search(text)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    if start < 0:
        raise ValueError("no JSON object found in the answer")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(candidate)):
        char = candidate[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return candidate[start : index + 1]
    raise ValueError("JSON object is not closed")


def parse_json_model(text: str, model: type[ModelT]) -> ModelT:
    """Parse ``text`` into ``model``; raises ``ValueError``/``ValidationError`` if invalid."""
    try:
        data = json.loads(extract_json_object(text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON: {exc.msg}") from exc
    return model.model_validate(data)


def tagged(tag: str, text: str) -> str:
    """Fence ``text`` in ``<tag>`` ... ``</tag>``, removing any tag lookalikes inside it."""
    safe = re.sub(rf"</?\s*{re.escape(tag)}\s*>", "", text, flags=re.IGNORECASE)
    return f"<{tag}>\n{safe}\n</{tag}>"


def extract_tag(tag: str, text: str) -> str | None:
    """Return the content of the first ``<tag>`` block, or None."""
    match = re.search(rf"<{re.escape(tag)}>\n?(.*?)\n?</{re.escape(tag)}>", text, re.DOTALL)
    return match.group(1) if match else None
