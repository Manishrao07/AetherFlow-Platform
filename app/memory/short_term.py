"""Short-term memory: the slice of a conversation an agent actually sees.

What: ``ShortTermMemory.build`` returns a ``ContextWindow`` = a rolling SUMMARY of old messages
plus the most recent messages verbatim, sized to a token budget.
Why sliding window + summary: sending the whole history grows cost and latency without bound
and eventually overflows the model's context. Recent turns matter most (keep them exact);
older turns matter as gist (compress them).
How: when summary + unsummarised messages exceed the budget, everything except the newest
``keep_recent`` messages is folded into the summary by one LLM call, and the fold position is
stored on the conversation (``summarized_until_message_count``) so it is never done twice.
Resilience: if the summariser fails, we still return a valid window (oldest messages dropped,
nothing persisted) instead of failing the user's request.
Token counts are estimates (~4 chars/token); the budget is a guardrail, not an invoice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import tagged
from app.core.llm.base import LLMError, LLMMessage, LLMRequest
from app.core.llm.client import CallContext, LLMClient
from app.core.untrusted import UNTRUSTED_NOTICE, wrap_untrusted
from app.models import Conversation, Message
from app.models.enums import LLMCaller, MessageRole
from app.services.chunking import estimate_tokens

logger = logging.getLogger(__name__)

_LLM_ROLES = {MessageRole.USER, MessageRole.ASSISTANT}


@dataclass(frozen=True)
class ContextWindow:
    summary: str
    messages: list[LLMMessage]
    token_estimate: int
    newly_summarized: int  # how many messages this call folded into the summary


class ShortTermMemory:
    def __init__(
        self,
        session: AsyncSession,
        llm: LLMClient,
        *,
        token_budget: int,
        keep_recent: int,
        summary_max_tokens: int,
    ) -> None:
        self._session = session
        self._llm = llm
        self._budget = token_budget
        self._keep_recent = keep_recent
        self._summary_max_tokens = summary_max_tokens

    async def build(self, conversation: Conversation) -> ContextWindow:
        """Return the context window for ``conversation``, summarising old messages if needed."""
        rows = await self._unsummarized(conversation)
        summary = conversation.summary
        folded = 0
        if self._tokens(summary, rows) > self._budget and len(rows) > self._keep_recent:
            to_fold, rows_after = rows[: -self._keep_recent], rows[-self._keep_recent :]
            new_summary = await self._summarise(summary, to_fold, conversation)
            if new_summary is not None:
                summary, rows, folded = new_summary, rows_after, len(to_fold)
                conversation.summary = summary
                conversation.summarized_until_message_count += folded
        window = self._fit(summary, rows)
        total = estimate_tokens(summary) + sum(self._msg_tokens(m) for m in window)
        return ContextWindow(
            summary, [LLMMessage(_role(m), m.content) for m in window], total, folded
        )

    async def _unsummarized(self, conversation: Conversation) -> list[Message]:
        """Messages not yet folded into the summary, oldest first."""
        result = await self._session.scalars(
            sa.select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at, Message.id)
            .offset(conversation.summarized_until_message_count)
        )
        return list(result)

    def _msg_tokens(self, message: Message) -> int:
        return message.token_count or estimate_tokens(message.content)

    def _tokens(self, summary: str, rows: list[Message]) -> int:
        return estimate_tokens(summary) + sum(
            self._msg_tokens(m) for m in rows if m.role in _LLM_ROLES
        )

    def _fit(self, summary: str, rows: list[Message]) -> list[Message]:
        """Newest messages that fit the remaining budget (always keeps at least the newest)."""
        usable = [m for m in rows if m.role in _LLM_ROLES]
        remaining = max(self._budget - estimate_tokens(summary), 1)
        kept: list[Message] = []
        used = 0
        for message in reversed(usable):
            cost = self._msg_tokens(message)
            if kept and used + cost > remaining:
                break
            kept.append(message)
            used += cost
        return list(reversed(kept))

    async def _summarise(
        self, previous: str, rows: list[Message], conversation: Conversation
    ) -> str | None:
        """One LLM call folding ``rows`` into the summary; None if it failed."""
        transcript = "\n".join(f"{m.role.value}: {m.content}" for m in rows if m.role in _LLM_ROLES)
        request = LLMRequest(
            system=(
                "You compress chat history. Write a faithful summary of the conversation in at "
                f"most {self._summary_max_tokens} tokens, keeping names, numbers, decisions and "
                "open questions. Reply with the summary text only. " + UNTRUSTED_NOTICE
            ),
            messages=[
                LLMMessage(
                    "user",
                    tagged("previous_summary", previous)
                    + "\n\n"
                    + tagged("messages", wrap_untrusted(transcript, "chat history")),
                )
            ],
            task="summarize_conversation",
            max_tokens=self._summary_max_tokens,
        )
        try:
            response = await self._llm.complete(request, CallContext(LLMCaller.MEMORY.value))
        except LLMError:
            logger.warning(
                "conversation summarisation failed; using truncated window",
                extra={"conversation_id": str(conversation.id)},
            )
            return None
        # WHY a hard cap: the summary is stored and re-sent every time; it must not grow.
        return response.text.strip()[: self._summary_max_tokens * 4]


def _role(message: Message) -> Literal["user", "assistant"]:
    return "assistant" if message.role is MessageRole.ASSISTANT else "user"
