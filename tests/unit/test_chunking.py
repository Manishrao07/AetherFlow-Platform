"""Chunker: respects size, keeps content, overlaps, validates input."""

import pytest

from app.services.chunking import chunk_text, estimate_tokens


def test_short_text_is_single_chunk() -> None:
    assert chunk_text("Hello world.") == ["Hello world."]


def test_empty_text_gives_no_chunks() -> None:
    assert chunk_text("  \n\n  ") == []


def test_chunks_never_exceed_max_chars() -> None:
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(10))
    chunks = chunk_text(text, max_chars=300, overlap=40)
    assert len(chunks) > 3
    assert all(len(c) <= 300 for c in chunks)


def test_overlap_repeats_text_between_neighbours() -> None:
    text = "\n\n".join(f"alpha{i} " + "x" * 80 for i in range(6))
    chunks = chunk_text(text, max_chars=200, overlap=60)
    assert len(chunks) >= 2
    tail_word = chunks[0].split()[-1]
    assert tail_word in chunks[1]


def test_very_long_paragraph_is_split_at_word_boundaries() -> None:
    chunks = chunk_text("lorem " * 500, max_chars=100, overlap=0)
    assert all(len(c) <= 100 for c in chunks)
    assert sum(c.count("lorem") for c in chunks) == 500


@pytest.mark.parametrize("max_chars,overlap", [(0, 0), (100, -1), (100, 100)])
def test_invalid_parameters(max_chars: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        chunk_text("text", max_chars=max_chars, overlap=overlap)


def test_estimate_tokens() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
