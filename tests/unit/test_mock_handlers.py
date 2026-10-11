"""The deterministic mock-LLM handlers."""

import json

from app.agents.base import tagged
from app.agents.mock_handlers import (
    plan_handler,
    register_mock_handlers,
    retrieval_handler,
    summary_handler,
)
from app.core.llm.base import LLMMessage, LLMRequest
from app.core.llm.mock_provider import MockProvider


def request(text: str, task: str = "plan", extra: list[LLMMessage] | None = None) -> LLMRequest:
    return LLMRequest(system="s", messages=[LLMMessage("user", text), *(extra or [])], task=task)


def test_plan_is_valid_json_with_sequential_keys() -> None:
    plan = json.loads(plan_handler(request(tagged("goal", "open a ticket about the outage"))))
    assert [s["key"] for s in plan["steps"]] == ["s1", "s2"]
    assert plan["steps"][1]["tool"] == "create_ticket"
    assert plan["steps"][1]["inputs"]["description"] == "{{s1}}"


def test_plan_falls_back_to_default_recipient() -> None:
    plan = json.loads(plan_handler(request(tagged("goal", "please email the team"))))
    assert plan["steps"][1]["inputs"]["recipient"] == "team@example.com"


def test_bad_json_hook_only_misbehaves_on_first_attempt() -> None:
    goal = tagged("goal", "refunds [force-bad-json]")
    assert "{" not in plan_handler(request(goal))
    repaired = request(goal, extra=[LLMMessage("assistant", "x"), LLMMessage("user", "fix")])
    assert json.loads(plan_handler(repaired))["steps"]


def test_retrieval_handler_without_passages() -> None:
    out = json.loads(retrieval_handler(request("<question>\nq\n</question>", "retrieve_answer")))
    assert out["citations"] == []


TRAVEL = (
    "Employees may book economy flights only for trips under 6 hours. "
    "Business class requires written approval from a director. "
    "Hotel stays are capped at 150 dollars per night."
)
REFUND = "Refunds are issued to the original payment method within 5 to 7 business days."


def answer_for(question: str, *passages: str) -> dict[str, object]:
    blocks = "\n\n".join(
        f'<untrusted_data source="P{i}">\nTitle: T{i}\n\n{text}\n</untrusted_data>'
        for i, text in enumerate(passages, 1)
    )
    prompt = f"<question>\n{question}\n</question>\n\n{blocks}"
    return json.loads(retrieval_handler(request(prompt, "retrieve_answer")))  # type: ignore[no-any-return]


def test_retrieval_quotes_the_sentence_that_matches_the_question() -> None:
    out = answer_for("What is the hotel limit per night?", TRAVEL)
    assert "Hotel stays are capped at 150 dollars per night." in str(out["answer"])
    assert "economy flights" not in str(out["answer"])


def test_retrieval_prefers_adjacent_question_words() -> None:
    out = answer_for("Can I book business class flights?", TRAVEL)
    assert "Business class requires written approval" in str(out["answer"])


def test_retrieval_drops_passages_that_share_only_a_stray_word() -> None:
    out = answer_for("Can I book business class flights?", TRAVEL, REFUND)
    assert out["citations"] == ["P1"]


def test_retrieval_understands_money_back_as_refund() -> None:
    text = "Refunds are issued within 5 to 7 days. Digital goods are final."
    out = answer_for("How long does it take to get my money back?", text)
    assert "Refunds are issued within 5 to 7 days." in str(out["answer"])


def test_retrieval_falls_back_to_the_top_passage_when_nothing_matches() -> None:
    out = answer_for("zzz qqq", TRAVEL, REFUND)
    assert out["citations"] == ["P1"]


def test_summary_handler_is_bounded_and_keeps_the_newest_information() -> None:
    text = tagged("previous_summary", "earlier gist") + tagged(
        "messages", "\n".join(f"user: message number {i} " + "x" * 200 for i in range(50))
    )
    small = LLMRequest(
        system="s",
        messages=[LLMMessage("user", text)],
        task="summarize_conversation",
        max_tokens=100,
    )
    out = summary_handler(small)
    assert len(out) <= 400 and "message number 49" in out and "earlier gist" not in out
    roomy = LLMRequest(system="s", messages=[LLMMessage("user", text)], max_tokens=5000)
    assert summary_handler(roomy).startswith("earlier gist")


def test_registration_is_a_noop_for_real_providers() -> None:
    class Fake:
        name = "anthropic"

    assert register_mock_handlers(Fake()) is False  # type: ignore[arg-type]
    mock = MockProvider()
    assert register_mock_handlers(mock) is True


def test_plan_deep_handler_strips_the_leading_verb_and_honours_the_count() -> None:
    from app.agents.mock_handlers import plan_deep_handler

    request = LLMRequest(
        system="Break the topic into 3 distinct sub-questions.",
        messages=[LLMMessage("user", "<goal>\nDeep research on solar power?\n</goal>")],
        task="plan_deep",
    )
    data = json.loads(plan_deep_handler(request))
    assert len(data["sub_questions"]) == 3
    assert data["summary"] == "Deep research: solar power"
    assert "What is solar power" in data["sub_questions"][0]
