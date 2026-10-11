"""Embedders: the offline hashing embedder and the OpenAI-compatible HTTP one."""

import json

import httpx
import pytest

from app.core.config import EmbeddingProviderName
from app.core.embeddings import (
    EmbeddingError,
    HashingEmbedder,
    OpenAICompatEmbedder,
    build_embedder,
    cosine,
)
from app.services.synonyms import expand_query
from tests.conftest import make_settings


async def sim(embedder: HashingEmbedder, a: str, b: str) -> float:
    va, vb = await embedder.embed([a, b])
    return cosine(va, vb)


async def test_hashing_is_deterministic_and_unit_length() -> None:
    e = HashingEmbedder()
    (v1,), (v2,) = (
        await e.embed(["Refund policy for digital goods"]),
        await e.embed(["Refund policy for digital goods"]),
    )
    assert v1 == v2 and len(v1) == 512
    assert sum(x * x for x in v1) == pytest.approx(1.0)


async def test_related_text_scores_higher_than_unrelated() -> None:
    e = HashingEmbedder()
    related = await sim(e, "refund window for digital goods", "digital goods refund rules")
    unrelated = await sim(e, "refund window for digital goods", "hotel stays are capped nightly")
    assert related > 0.5 > unrelated


async def test_synonym_expander_bridges_paraphrases() -> None:
    plain, smart = HashingEmbedder(), HashingEmbedder(expand=expand_query)
    q, doc = "how do I get my money back", "refund issued to the original payment method"
    assert await sim(smart, q, doc) > await sim(plain, q, doc)
    assert plain.name != smart.name  # vectors from the two variants must never be mixed


async def test_empty_text_gives_a_zero_vector_and_zero_similarity() -> None:
    (vec,) = await HashingEmbedder().embed(["the of and"])
    assert not any(vec) and cosine(vec, vec) == 0.0


def test_cosine_edge_cases() -> None:
    assert cosine([], []) == 0.0
    assert cosine([1.0, 0.0], [1.0]) == 0.0
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def make_remote(handler: object) -> OpenAICompatEmbedder:
    return OpenAICompatEmbedder(
        "https://emb.test/v1",
        "k",
        "m1",
        5.0,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


async def test_remote_embedder_orders_by_index_and_normalises() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        data = [
            {"index": 1, "embedding": [0.0, 2.0]},
            {"index": 0, "embedding": [3.0, 0.0]},
        ]
        return httpx.Response(200, json={"data": data})

    vectors = await make_remote(handler).embed(["a", "b"])
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert seen["auth"] == "Bearer k" and seen["body"] == {"model": "m1", "input": ["a", "b"]}


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={}),
        httpx.Response(200, json={"oops": 1}),
        httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]}),
    ],
)
async def test_remote_failures_raise_embedding_error(response: httpx.Response) -> None:
    with pytest.raises(EmbeddingError):
        await make_remote(lambda _r: response).embed(["a", "b"])


async def test_remote_embedder_reports_connection_errors() -> None:
    def boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(EmbeddingError):
        await make_remote(boom).embed(["a"])
    assert await make_remote(boom).embed([]) == []


def test_builder_follows_the_setting() -> None:
    assert build_embedder(make_settings(embedding_provider="none")) is None
    assert isinstance(build_embedder(make_settings()), HashingEmbedder)
    remote = build_embedder(
        make_settings(embedding_provider="openai", embedding_model="m", embedding_api_key="k")
    )
    assert isinstance(remote, OpenAICompatEmbedder) and remote.name == "openai:m"


def test_openai_embeddings_need_a_model_and_a_key() -> None:
    with pytest.raises(ValueError, match="EMBEDDING_MODEL"):
        make_settings(embedding_provider=EmbeddingProviderName.OPENAI, embedding_api_key="k")
    with pytest.raises(ValueError, match="EMBEDDING_API_KEY"):
        make_settings(embedding_provider=EmbeddingProviderName.OPENAI, embedding_model="m")
