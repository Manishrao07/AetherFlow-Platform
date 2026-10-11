"""Provider-agnostic LLM types and the error taxonomy.

What: request/response dataclasses, the ``LLMProvider`` interface, and exceptions that say
whether a failure is worth retrying.
Why an interface: agents depend on ``LLMProvider``, never on a vendor SDK. Swapping Anthropic
for another vendor (or the deterministic mock) changes one object, not the agents.
Why an error taxonomy: retry logic must distinguish "try again" (timeout, 429, 5xx) from
"never going to work" (bad request, bad API key). Retrying the latter wastes money and time.
Connects to: ``client.py`` (retry + breaker around a provider), agents (Phase 4).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class LLMMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class LLMRequest:
    """One completion request.

    Sampling parameters (temperature, ...) are deliberately absent: recent models fix or
    deprecate them, so exposing them would invite requests the API rejects.

    ``task`` is a short label ("plan", "review", "summarize", ...) used for logging, metrics
    and by the mock provider to pick a deterministic handler. Real providers ignore it.
    """

    system: str
    messages: list[LLMMessage]
    task: str = "chat"
    max_tokens: int = 1024
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int = 0
    cost_usd: Decimal = Decimal(0)
    cached: bool = False
    stop_reason: str | None = None


class LLMProvider(ABC):
    """What every provider (Anthropic, mock, ...) must implement."""

    name: str
    model: str

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Run one completion. Raise an ``LLMError`` subclass on failure."""


# --------------------------------------------------------------------- errors
class LLMError(Exception):
    """Base class. ``retryable`` drives the retry loop; ``retry_after`` overrides backoff."""

    retryable = False

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LLMTimeoutError(LLMError):
    retryable = True


class LLMRateLimitError(LLMError):
    retryable = True


class LLMServerError(LLMError):
    retryable = True


class LLMConnectionError(LLMError):
    retryable = True


class LLMBadRequestError(LLMError):
    """Our request is invalid (HTTP 400/413/422). Retrying cannot help."""


class LLMAuthError(LLMError):
    """Bad or missing credentials. Retrying cannot help; needs an operator."""


class CircuitOpenError(LLMError):
    """Fast-fail: the breaker is open because the provider has been failing."""

    retryable = False
