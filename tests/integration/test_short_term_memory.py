"""Short-term memory: sliding window + rolling summary against a real database."""

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.mock_handlers import register_mock_handlers
from app.core.llm.base import LLMBadRequestError
from app.core.llm.client import build_llm_client
from app.core.llm.mock_provider import MockProvider
from app.memory.short_term import ShortTermMemory
from app.models import Conversation, LLMCall, User
from app.models.enums import MessageRole
from app.services.conversations import ConversationService
from app.services.llm_usage import DbUsageRecorder
from app.services.seeding import seed_demo_data
from tests.conftest import DEMO_PASSWORD, ROOT, make_settings


@pytest.fixture
async def setup(session_factory: async_sessionmaker[AsyncSession]):
    await seed_demo_data(session_factory, DEMO_PASSWORD, ROOT / "scripts" / "sample_docs")
    llm = build_llm_client(make_settings(), DbUsageRecorder(session_factory))
    register_mock_handlers(llm.provider)
    async with session_factory() as s:
        user_id = await s.scalar(sa.select(User.id).where(User.email == "operator@demo.local"))
    assert user_id is not None
    return llm, user_id, session_factory


async def fill(session: AsyncSession, user_id, count: int, size: int = 200) -> Conversation:
    service = ConversationService(session)
    conversation = await service.create(user_id, "chat")
    for i in range(count):
        role = MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT
        await service.append(user_id, conversation.id, f"msg{i:02d} " + "w" * size, role)
    await session.commit()
    return conversation


def memory(session: AsyncSession, llm, budget: int = 300, keep: int = 3) -> ShortTermMemory:
    return ShortTermMemory(
        session, llm, token_budget=budget, keep_recent=keep, summary_max_tokens=100
    )


async def test_short_conversation_is_returned_verbatim(setup) -> None:
    llm, user_id, sf = setup
    async with sf() as s:
        conversation = await fill(s, user_id, 4, size=20)
        window = await memory(s, llm, budget=2000).build(conversation)
    assert window.summary == "" and window.newly_summarized == 0
    assert [m.content[:5] for m in window.messages] == ["msg00", "msg01", "msg02", "msg03"]
    assert [m.role for m in window.messages] == ["user", "assistant", "user", "assistant"]
    assert llm.provider.calls == []  # type: ignore[attr-defined]


async def test_long_conversation_is_summarised_once_and_persisted(setup) -> None:
    llm, user_id, sf = setup
    async with sf() as s:
        conversation = await fill(s, user_id, 10)
        stm = memory(s, llm)
        window = await stm.build(conversation)
        await s.commit()
        assert window.newly_summarized == 7 and "msg00" in window.summary
        assert [m.content[:5] for m in window.messages] == ["msg07", "msg08", "msg09"]
        assert window.token_estimate <= 300 + 60  # within budget (+ estimate slack)
        assert conversation.summarized_until_message_count == 7
        # second call: nothing new to fold, no extra LLM call
        calls_before = len(llm.provider.calls)  # type: ignore[attr-defined]
        again = await stm.build(conversation)
        assert again.newly_summarized == 0 and again.summary == window.summary
        assert len(llm.provider.calls) == calls_before  # type: ignore[attr-defined]
    async with sf() as s:
        stored = await s.get(Conversation, conversation.id)
        agents = (await s.scalars(sa.select(LLMCall.agent))).all()
    assert stored is not None and stored.summarized_until_message_count == 7
    assert [a.value for a in agents] == ["memory"]


async def test_new_messages_after_a_summary_extend_it(setup) -> None:
    llm, user_id, sf = setup
    async with sf() as s:
        conversation = await fill(s, user_id, 10)
        await memory(s, llm).build(conversation)
        service = ConversationService(s)
        for i in range(10, 16):
            await service.append(user_id, conversation.id, f"msg{i} " + "w" * 200)
        window = await memory(s, llm).build(conversation)
    assert window.newly_summarized > 0
    assert "msg12" in window.summary and len(window.summary) <= 400
    assert window.messages[-1].content.startswith("msg15")


async def test_summariser_failure_degrades_to_truncated_window(setup) -> None:
    llm, user_id, sf = setup
    provider = llm.provider
    assert isinstance(provider, MockProvider)
    provider.fail_next(LLMBadRequestError("rejected"))
    async with sf() as s:
        conversation = await fill(s, user_id, 10)
        window = await memory(s, llm).build(conversation)
    assert window.summary == "" and window.newly_summarized == 0
    assert window.messages and window.messages[-1].content.startswith("msg09")
    assert window.token_estimate <= 300 + 60
    assert conversation.summarized_until_message_count == 0  # nothing persisted


async def test_one_huge_message_still_yields_a_window(setup) -> None:
    llm, user_id, sf = setup
    async with sf() as s:
        conversation = await fill(s, user_id, 1, size=8000)
        window = await memory(s, llm).build(conversation)
    assert len(window.messages) == 1  # newest message is always kept


async def test_system_and_tool_messages_are_not_sent_to_the_model(setup) -> None:
    llm, user_id, sf = setup
    async with sf() as s:
        service = ConversationService(s)
        conversation = await service.create(user_id, "chat")
        await service.append(user_id, conversation.id, "hello")
        await service.append(user_id, conversation.id, "internal", MessageRole.SYSTEM)
        await service.append(user_id, conversation.id, "tool out", MessageRole.TOOL)
        await service.append(user_id, conversation.id, "hi there", MessageRole.ASSISTANT)
        window = await memory(s, llm, budget=2000).build(conversation)
    assert [m.content for m in window.messages] == ["hello", "hi there"]
