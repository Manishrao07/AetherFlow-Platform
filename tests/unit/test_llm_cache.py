"""LLM response caching: hits skip the provider, bad answers can be evicted."""

from app.core.cache import InMemoryCache
from app.core.llm.base import LLMMessage, LLMRequest, LLMServerError
from app.core.llm.circuit_breaker import CircuitBreaker
from app.core.llm.client import CallContext, LLMClient, UsageRecord, build_llm_client
from app.core.llm.mock_provider import MockProvider
from app.core.llm.retry import RetryPolicy
from tests.conftest import make_settings

CTX = CallContext(agent="planner")


class ListRecorder:
    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    async def record(self, usage: UsageRecord) -> None:
        self.records.append(usage)


def make_client(cache: InMemoryCache | None = None) -> tuple[LLMClient, MockProvider, ListRecorder]:
    provider = MockProvider()
    provider.register("chat", lambda r: f"answer #{len(provider.calls)}")
    recorder = ListRecorder()
    client = LLMClient(
        provider,
        retry_policy=RetryPolicy(max_attempts=1, base_delay=0, max_delay=0),
        breaker=CircuitBreaker("llm-cache-test", failure_threshold=1, recovery_seconds=60),
        timeout_seconds=1,
        recorder=recorder,
        cache=cache,
        cache_ttl_seconds=60,
    )
    return client, provider, recorder


def request(text: str = "hello", task: str = "chat") -> LLMRequest:
    return LLMRequest(system="sys", messages=[LLMMessage("user", text)], task=task)


async def test_identical_requests_are_served_from_the_cache() -> None:
    client, provider, recorder = make_client(InMemoryCache())
    first = await client.complete(request(), CTX)
    second = await client.complete(request(), CTX)
    assert len(provider.calls) == 1
    assert first.cached is False and second.cached is True and second.text == first.text
    cached = recorder.records[-1]
    assert cached.cached is True and cached.prompt_tokens == 0 and cached.cost_usd == 0


async def test_different_prompts_do_not_share_entries() -> None:
    client, provider, _ = make_client(InMemoryCache())
    await client.complete(request("one"), CTX)
    await client.complete(request("two"), CTX)
    await client.complete(request("one", task="other"), CTX)
    assert len(provider.calls) == 3


async def test_cache_hits_work_while_the_provider_is_down() -> None:
    client, provider, _ = make_client(InMemoryCache())
    await client.complete(request(), CTX)
    provider.fail_next(LLMServerError("outage"))
    assert (await client.complete(request(), CTX)).cached is True
    assert provider._failures  # the failure was never consumed: the provider wasn't called


async def test_evict_forgets_a_bad_answer() -> None:
    client, provider, _ = make_client(InMemoryCache())
    await client.complete(request(), CTX)
    await client.evict(request())
    assert (await client.complete(request(), CTX)).cached is False
    assert len(provider.calls) == 2


async def test_a_corrupt_cache_entry_is_just_a_miss() -> None:
    cache = InMemoryCache()
    client, provider, _ = make_client(cache)
    await client.complete(request(), CTX)
    key = next(iter(cache._data))
    await cache.set(key, "not json", 60)
    assert (await client.complete(request(), CTX)).cached is False
    await cache.set(key, '{"text": "x"}', 60)  # valid JSON, missing fields
    assert (await client.complete(request(), CTX)).cached is False
    assert len(provider.calls) == 3


async def test_without_a_cache_nothing_is_cached() -> None:
    client, provider, _ = make_client(None)
    await client.complete(request(), CTX)
    await client.complete(request(), CTX)
    await client.evict(request())  # harmless without a cache
    assert len(provider.calls) == 2


async def test_the_setting_controls_whether_the_factory_wires_a_cache() -> None:
    cache = InMemoryCache()
    off = build_llm_client(make_settings(llm_cache_enabled=False), cache=cache)
    on = build_llm_client(make_settings(llm_cache_enabled=True), cache=cache)
    for client in (off, on):
        await client.complete(request(task="plan"), CTX)
        await client.complete(request(task="plan"), CTX)
    assert len(off.provider.calls) == 2 and len(on.provider.calls) == 1  # type: ignore[attr-defined]
