"""{{step}} placeholders: detection and safe substitution."""

import pytest

from app.agents.templating import MAX_RENDERED_CHARS, placeholders_in, render_output, substitute


def test_placeholders_found_in_nested_structures() -> None:
    value = {"a": "x {{s1}} y", "b": ["{{ s2 }}", 5, {"c": "{{s1}}"}], "d": None}
    assert placeholders_in(value) == {"s1", "s2"}
    assert placeholders_in({}) == set() and placeholders_in([]) == set()
    assert placeholders_in(42) == set()


def test_retrieval_output_renders_as_cited_snippets() -> None:
    out = {"results": [{"citation": "Policy #1", "content": "Refunds in 30 days"}]}
    assert render_output(out) == "[Policy #1] Refunds in 30 days"


def test_answer_and_generic_outputs_render() -> None:
    assert render_output({"answer": "forty-two"}) == "forty-two"
    assert render_output({"result": 5}) == '{"result": 5}'


def test_rendered_text_is_capped() -> None:
    assert len(render_output({"answer": "z" * 10_000})) == MAX_RENDERED_CHARS


def test_substitute_replaces_in_nested_values() -> None:
    outputs = {"s1": {"answer": "hello"}}
    result = substitute({"t": "say {{s1}}!", "l": ["{{s1}}", 3], "n": 7}, outputs)
    assert result == {"t": "say hello!", "l": ["hello", 3], "n": 7}


def test_dummy_mode_needs_no_outputs() -> None:
    assert substitute({"t": "a {{s9}} b"}, {}, dummy=True) == {"t": "a x b"}


def test_missing_output_raises_key_error() -> None:
    with pytest.raises(KeyError):
        substitute("{{s1}}", {})


def test_substitution_is_text_only_no_evaluation() -> None:
    outputs = {"s1": {"answer": "{{s2}} __import__('os')"}}
    # the substituted text is NOT re-scanned for placeholders
    assert substitute("{{s1}}", outputs) == "{{s2}} __import__('os')"
