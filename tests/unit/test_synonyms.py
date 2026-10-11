"""Query expansion for keyword search."""

from app.services.synonyms import expand_query


def test_known_phrase_adds_synonyms_after_the_original_wording() -> None:
    out = expand_query("How long to get my money back?")
    assert out.startswith("How long to get my money back?")
    assert "refund" in out and "within" in out


def test_unknown_wording_is_returned_unchanged() -> None:
    assert expand_query("tell me about plants") == "tell me about plants"


def test_matching_is_on_whole_words_and_ignores_case() -> None:
    assert "capped" in expand_query("MAXIMUM allowed")
    assert expand_query("a pricey item") == "a pricey item"  # "price" is not inside "pricey"
