"""Text embeddings: turn text into vectors so "similar meaning" becomes "small distance".

What: an ``Embedder`` interface with two implementations and a cosine helper.
- ``HashingEmbedder``: deterministic, offline, no key. Words (and word pairs) are hashed into a
  fixed-size vector ("feature hashing"). It captures vocabulary overlap, and with a synonym
  expander ("money back" -> "refund") it also captures the paraphrases we list. It is NOT a
  trained language model: unseen paraphrases are not understood.
- ``OpenAICompatEmbedder``: calls any OpenAI-compatible ``/embeddings`` endpoint, which gives
  real semantic vectors when you provide a key and a model name.
Why an interface: the rest of the system only needs "texts in, vectors out", so the offline
embedder (tests, demos, no key) and a real one are swappable by configuration.
Why vectors are L2-normalised: cosine similarity is then a plain dot product.
Connects to: ``services/knowledge.py`` (hybrid search), ``services/documents.py`` (ingest),
``services/workflows.py`` (similar past research).
"""

from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from itertools import pairwise

import httpx

from app.core.config import EmbeddingProviderName, Settings

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
    ]
)


class EmbeddingError(Exception):
    """The embedding service failed; callers fall back to keyword search."""


class Embedder(ABC):
    """Texts in, equal-length unit vectors out."""

    name: str  # stored next to every vector: vectors from different models must never be mixed

    @abstractmethod
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per text, in order."""


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two vectors (0.0 if either is empty or the sizes differ)."""
    if not a or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class HashingEmbedder(Embedder):
    """Feature-hashing embedder: deterministic and dependency-free."""

    def __init__(self, dimensions: int = 512, expand: Callable[[str], str] | None = None) -> None:
        self._dim = dimensions
        self._expand = expand
        self.name = f"hash-{dimensions}" + ("-syn" if expand else "")

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def _vector(self, text: str) -> list[float]:
        words = [w for w in _WORD.findall((self._expand or str)(text).lower()) if w not in _STOP]
        # WHY 5-letter stems: "refund", "refunds", "refundable" share a feature.
        stems = [w[:5] for w in words]
        features = stems + [f"{a}_{b}" for a, b in pairwise(stems)]
        vec = [0.0] * self._dim
        for feature in features:
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            number = int.from_bytes(digest, "big")
            sign = 1.0 if number & 1 else -1.0  # WHY signs: collisions cancel instead of add up
            weight = 0.5 if "_" in feature else 1.0  # pairs help, but less than single words
            vec[(number >> 1) % self._dim] += sign * weight
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec


class OpenAICompatEmbedder(Embedder):
    """Real embeddings from an OpenAI-compatible ``POST {base_url}/embeddings`` endpoint."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/embeddings"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._model = model
        self._timeout = timeout_seconds
        self._transport = transport
        self.name = f"openai:{model}"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as http:
                response = await http.post(
                    self._url,
                    headers=self._headers,
                    json={"model": self._model, "input": list(texts)},
                )
            response.raise_for_status()
            rows = sorted(response.json()["data"], key=lambda r: r["index"])
            vectors = [[float(x) for x in row["embedding"]] for row in rows]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise EmbeddingError(f"embedding request failed: {type(exc).__name__}") from exc
        if len(vectors) != len(texts):
            raise EmbeddingError("embedding service returned the wrong number of vectors")
        return [_unit(v) for v in vectors]


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm else vector


def build_embedder(
    settings: Settings, expand: Callable[[str], str] | None = None
) -> Embedder | None:
    """The configured embedder, or ``None`` when semantic search is switched off."""
    if settings.embedding_provider is EmbeddingProviderName.NONE:
        return None
    if settings.embedding_provider is EmbeddingProviderName.OPENAI:
        assert settings.embedding_api_key is not None and settings.embedding_model  # validated
        return OpenAICompatEmbedder(
            settings.embedding_base_url,
            settings.embedding_api_key.get_secret_value(),
            settings.embedding_model,
            settings.embedding_timeout_seconds,
        )
    return HashingEmbedder(expand=expand)
