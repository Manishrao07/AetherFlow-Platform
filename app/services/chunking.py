"""Split documents into overlapping, paragraph-aware chunks.

What: ``chunk_text`` turns a long document into ~``max_chars`` pieces; ``estimate_tokens``
gives a cheap token estimate.
Why chunk at all: retrieval returns the few most relevant passages (with citations) instead
of whole documents, which keeps prompts small and answers grounded. Overlap stops a fact
that straddles a boundary from being lost.
Why paragraph-aware: cutting on paragraph boundaries keeps ideas intact, which improves
relevance versus blind fixed-size windows.
Connects to: the ingestion service (Phase 4) and the seed script.
"""

from __future__ import annotations

import math
import re


def estimate_tokens(text: str) -> int:
    """Rough token count (~4 characters per token for English). Good enough for budgeting."""
    return math.ceil(len(text) / 4)


def _split_long_paragraph(paragraph: str, max_chars: int) -> list[str]:
    """Break a paragraph longer than ``max_chars`` at word boundaries."""
    parts: list[str] = []
    while len(paragraph) > max_chars:
        cut = paragraph.rfind(" ", 0, max_chars)
        cut = cut if cut > 0 else max_chars
        parts.append(paragraph[:cut])
        paragraph = paragraph[cut:].lstrip()
    parts.append(paragraph)
    return parts


def _overlap_tail(chunk: str, overlap: int) -> str:
    """Last ``overlap`` characters of a chunk, trimmed to start at a word boundary."""
    if overlap <= 0:
        return ""
    tail = chunk[-overlap:]
    return tail[tail.find(" ") + 1 :] if " " in tail else tail


def chunk_text(text: str, max_chars: int = 1000, overlap: int = 150) -> list[str]:
    """Return chunks of at most ``max_chars`` characters with ``overlap`` carried over."""
    if max_chars <= 0 or overlap < 0 or overlap >= max_chars:
        raise ValueError("require max_chars > 0 and 0 <= overlap < max_chars")

    pieces: list[str] = []
    for paragraph in (p.strip() for p in re.split(r"\n\s*\n", text)):
        if paragraph:
            pieces.extend(_split_long_paragraph(paragraph, max_chars))

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}\n\n{piece}" if current else piece
        if len(candidate) <= max_chars:
            current = candidate
            continue
        chunks.append(current)
        seeded = f"{_overlap_tail(current, overlap)}\n\n{piece}".strip()
        # WHY: if overlap + piece would overflow, prefer a clean chunk over a too-big one.
        current = seeded if len(seeded) <= max_chars else piece
    if current:
        chunks.append(current)
    return chunks
