"""Deterministic answers for the mock LLM, one handler per agent task.

What: functions that read the SAME prompts the real model would get and return valid JSON.
Why live in ``agents/`` and not in ``core/llm/mock_provider.py``: the mock provider is generic
(reused by Project 2); knowing what a "plan" or a "review" looks like is Project 1 business
logic, so it is registered from here at startup (and only when ``LLM_PROVIDER=mock``).
Why keyword rules: the demo and tests must behave identically on every run with no API key.
Test hooks (documented in the README): ``[force-bad-json]`` makes the first planner answer
invalid (exercises the repair path), ``[force-revision]`` makes the first review ask for a
revision, ``[force-reject]`` makes the review reject.
"""

from __future__ import annotations

import json
import re
from itertools import pairwise
from typing import Any

from app.agents.base import extract_tag
from app.core.llm.base import LLMProvider, LLMRequest
from app.core.llm.mock_provider import MockProvider
from app.services.synonyms import expand_query

_EXPRESSION = re.compile(r"\d[\d\s.()]*[-+*/%][\d\s.+\-*/%()]*\d")
_EMAIL = re.compile(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;.]+")
_URL = re.compile(r"https?://[^\s,;)]+")
_REMEMBER = re.compile(r"remember that (.+?)(?:[.\n]|$)", re.IGNORECASE)
_UNTRUSTED = re.compile(r'<untrusted_data source="([^"]*)">\n(.*?)\n</untrusted_data>', re.DOTALL)


def _first_user_text(request: LLMRequest) -> str:
    # WHY the FIRST message: on a repair attempt the last message is the error notice.
    return request.messages[0].content if request.messages else ""


def plan_handler(request: LLMRequest) -> str:
    """Keyword-driven plan: always search, then optionally calculate/ticket/notify/remember."""
    text = _first_user_text(request)
    goal = " ".join((extract_tag("goal", text) or "").split())
    if "[force-bad-json]" in goal and len(request.messages) == 1:
        return "Sure! Here is the plan: (no JSON)"
    lower = goal.lower()
    steps: list[dict[str, Any]] = []

    def add(
        agent: str, tool: str, inputs: dict[str, Any], description: str, deps: list[str],
        risk: str = "LOW", approval: bool = False,
    ) -> str:  # fmt: skip
        key = f"s{len(steps) + 1}"
        steps.append(
            {
                "key": key, "description": description, "agent": agent, "tool": tool,
                "inputs": inputs, "depends_on": deps, "requires_approval": approval,
                "risk_level": risk,
            }
        )  # fmt: skip
        return key

    search = add(
        "retrieval", "search_knowledge_base", {"query": goal[:500], "top_k": 5},
        "Search the knowledge base for relevant information", [],
    )  # fmt: skip
    if expression := _EXPRESSION.search(goal):
        add("execution", "calculator", {"expression": expression.group(0).strip()},
            "Calculate the requested value", [])  # fmt: skip
    if re.search(r"\b(ticket|incident)\b", lower):
        severity = "SEV1" if "sev1" in lower else "SEV2" if "sev2" in lower else "SEV3"
        add("execution", "create_ticket",
            {"title": goal[:120], "description": f"{{{{{search}}}}}", "severity": severity},
            "Open a ticket summarising the findings", [search], risk="MEDIUM")  # fmt: skip
    if re.search(r"\b(notify|email|e-mail|notification)\b", lower):
        recipient = _EMAIL.search(goal)
        add("execution", "send_notification",
            {"recipient": recipient.group(0) if recipient else "team@example.com",
             "subject": "Workflow update", "body": f"{{{{{search}}}}}"},
            "Notify the recipient with the findings", [search],
            risk="HIGH", approval=True)  # fmt: skip
    if remember := _REMEMBER.search(goal):
        fact = remember.group(1).strip()
        key_words = re.sub(r"[^a-z0-9]+", "_", fact.lower()).strip("_")[:40] or "note"
        add("execution", "memory_write", {"key": key_words, "content": fact},
            "Remember the stated fact", [])  # fmt: skip
    if url := _URL.search(goal):
        add("execution", "http_fetch", {"url": url.group(0)},
            "Fetch the referenced web page", [], risk="MEDIUM")  # fmt: skip
    return json.dumps({"summary": f"Plan for: {goal[:150]}", "steps": steps})


_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from",
        "how", "i", "in", "is", "it", "of", "on", "or", "the", "to", "what", "when", "where",
        "which", "who", "why", "will", "with", "you", "your",
    }
)  # fmt: skip


def _terms(text: str) -> set[str]:
    """Lower-case content words; the first 5 letters stand in for stemming (refund ~ refundable)."""
    return {w[:5] for w in _WORD.findall(text.lower()) if w not in _STOPWORDS}


def _phrases(question: str) -> list[str]:
    """Adjacent content-word pairs of the question ("business class"), used as a tie-breaker."""
    words = [w for w in _WORD.findall(question.lower()) if w not in _STOPWORDS]
    return [f"{a} {b}" for a, b in pairwise(words)]


def _best_sentence(content: str, question_terms: set[str], phrases: list[str]) -> tuple[int, str]:
    """The sentence sharing the most words with the question (earliest wins a tie).

    WHY a phrase bonus: "business class" should prefer the sentence where the two words sit
    together over one that merely contains each word somewhere.
    """
    # WHY drop heading lines: "# Refund Policy" is a title, never an answer.
    body = " ".join(ln for ln in content.splitlines() if not ln.lstrip().startswith("#"))
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", " ".join(body.split())) if s]
    if not sentences:
        return 0, ""
    scored = [
        (len(_terms(s) & question_terms) + sum(p in s.lower() for p in phrases), s)
        for s in sentences
    ]
    return max(scored, key=lambda pair: pair[0])


def _quote_best(request: LLMRequest) -> list[tuple[int, str, str]]:
    """Pick the best-matching sentence of the top passages -> ``[(score, passage_id, sentence)]``.

    WHY sentence selection by word overlap: copying each passage's first sentence answered
    "hotel limit" with the flights rule. Overlap is still keyword matching, not understanding,
    but it picks the sentence that actually contains the asked-about words.
    """
    text = _first_user_text(request)
    passages = _UNTRUSTED.findall(text)
    if not passages:
        return []
    question = extract_tag("question", text) or ""
    # WHY expand here too: the search already used the synonyms, so sentence choice must agree.
    question_terms, phrases = _terms(expand_query(question)), _phrases(question)
    candidates = []
    for passage_id, body in passages[:3]:
        score, sentence = _best_sentence(body.split("\n\n", 1)[-1], question_terms, phrases)
        candidates.append((score, passage_id, sentence))
    # Keep passages that share words with the question; if none do, fall back to the top passage.
    best = max(c[0] for c in candidates)
    # WHY "more than half of the best": a passage sharing one stray word ("business days") is noise.
    relevant = [c for c in candidates if c[0] * 2 > best] if best else candidates[:1]
    return sorted(relevant, key=lambda c: -c[0])[:2]


def retrieval_handler(request: LLMRequest) -> str:
    """Quote the best-matching sentence of the top passages and cite them by id."""
    chosen = _quote_best(request)
    if not chosen:
        return json.dumps({"answer": "The passages do not contain an answer.", "citations": []})
    snippets = [f"{sentence[:300]} [{pid}]" for _, pid, sentence in chosen]
    return json.dumps(
        {"answer": "According to the knowledge base: " + " ".join(snippets),
         "citations": [pid for _, pid, _ in chosen]}
    )  # fmt: skip


def research_handler(request: LLMRequest) -> str:
    """Deep research, mock: quote the best sentence of the top sources and cite their ids."""
    chosen = _quote_best(request)
    if not chosen:
        return json.dumps({"findings": "The passages do not contain an answer.", "citations": []})
    return json.dumps(
        {"findings": " ".join(f"{sentence[:300]} [{pid}]" for _, pid, sentence in chosen),
         "citations": [pid for _, pid, _ in chosen]}
    )  # fmt: skip


_LEADING_VERB = re.compile(
    r"^\s*(?:please\s+)?(?:do\s+)?(?:a\s+)?(?:deep\s+)?(?:research|investigate|explain|report)"
    r"(?:\s+(?:on|about|into))?\s+",
    re.IGNORECASE,
)
_QUESTION_COUNT = re.compile(r"into (\d+) distinct")


_COMPARE = re.compile(
    r"^\s*(?:compare\s+)?(.+?)\s+(?:vs\.?|versus|and|with)\s+(.+?)\s*$", re.IGNORECASE
)


def plan_deep_handler(request: LLMRequest) -> str:
    """Deep-research plan, mock: fixed angles on the topic (background, facts, uses, risks).

    A goal like "compare A and B" / "A vs B" gets one question per side plus two direct
    comparison questions instead.
    """
    goal = " ".join((extract_tag("goal", _first_user_text(request)) or "").split())
    topic = (_LEADING_VERB.sub("", goal).strip(" ?.") or goal)[:150]
    count = int(m.group(1)) if (m := _QUESTION_COUNT.search(request.system)) else 4
    sides = (
        _COMPARE.match(topic)
        if re.search(r"\bcompare\b|\bvs\.?\b|\bversus\b", goal, re.I)
        else None
    )
    if sides:
        a, b = sides.group(1).strip(), sides.group(2).strip()
        pairs = [
            f"What is {a} and what are its main strengths?",
            f"What is {b} and what are its main strengths?",
            f"How do {a} and {b} differ in key features?",
            f"When should one choose {a} over {b}, or the reverse?",
        ]
        return json.dumps({"summary": f"Comparison: {a} vs {b}", "sub_questions": pairs[:count]})
    angles = [
        f"What is {topic} and why does it matter?",
        f"What are the main facts and key rules about {topic}?",
        f"What are the benefits and typical uses of {topic}?",
        f"What are the risks, limits and open issues around {topic}?",
        f"What are recent developments related to {topic}?",
        f"How does {topic} compare with alternatives?",
    ]
    return json.dumps({"summary": f"Deep research: {topic}", "sub_questions": angles[:count]})


_SUB_QUESTION = re.compile(
    r"Sub-question \d+: (.*?)\nFindings: (.*?)(?=\n\nSub-question|\Z)", re.DOTALL
)
_CITE_NUMBER = re.compile(r"\[(\d{1,3})\]")


def synthesize_handler(request: LLMRequest) -> str:
    """Report writer, mock: a title, a summary and one section per sub-question."""
    text = _first_user_text(request)
    topic = " ".join((extract_tag("topic", text) or "the topic").split())
    blocks = _UNTRUSTED.findall(text)
    pairs = _SUB_QUESTION.findall(blocks[0][1]) if blocks else []
    sections = [f"## {q.strip()}\n{f.strip()}" for q, f in pairs]
    lead = pairs[0][1].strip() if pairs else "No findings were provided."
    report = f"# Research report: {topic}\n\n## Summary\n{lead}\n\n" + "\n\n".join(sections)
    cited = sorted({int(n) for n in _CITE_NUMBER.findall(report)})
    return json.dumps({"report": report, "citations": cited})


# A sentence plus any citation markers that trail its full stop: "... night. [2]".
_REPORT_SENTENCE = re.compile(r"[^.!?]+[.!?](?:\s*\[\d{1,3}\])*")


def followup_handler(request: LLMRequest) -> str:
    """Follow-up answer, mock: the report sentences that share most words with the question."""
    text = _first_user_text(request)
    question = extract_tag("question", text) or ""
    blocks = _UNTRUSTED.findall(text)
    report = blocks[0][1] if blocks else ""
    lines = (ln for ln in report.splitlines() if ln.strip() and not ln.lstrip().startswith("#"))
    found = (m.strip() for ln in lines for m in _REPORT_SENTENCE.findall(ln))
    sentences = list(dict.fromkeys(s for s in found if len(s) > 20))
    terms, phrases = _terms(expand_query(question)), _phrases(question)
    scored = sorted(
        ((len(_terms(s) & terms) + sum(p in s.lower() for p in phrases), s) for s in sentences),
        key=lambda pair: -pair[0],
    )
    top = [s for score, s in scored[:2] if score > 0]
    if not top:
        return json.dumps({"answer": "The report does not cover that question.", "citations": []})
    answer = " ".join(top)
    return json.dumps(
        {"answer": answer, "citations": sorted({int(n) for n in _CITE_NUMBER.findall(answer)})}
    )


def review_handler(request: LLMRequest) -> str:
    """Approve when every step succeeded; hooks in the goal force other verdicts."""
    text = _first_user_text(request)
    goal = extract_tag("goal", text) or ""
    revised = extract_tag("previous_review_feedback", text) is not None
    blocks = _UNTRUSTED.findall(text)
    results: list[dict[str, Any]] = json.loads(blocks[0][1]) if blocks else []
    failed = [r["key"] for r in results if r.get("status") != "SUCCEEDED"]
    if "[force-reject]" in goal:
        verdict, score, reasons, feedback = "REJECTED", 0.1, ["The goal violates policy."], ""
    elif failed:
        verdict, score = "NEEDS_REVISION", 0.4
        reasons = [f"Steps did not succeed: {', '.join(failed)}."]
        feedback = "Plan the failed steps differently or use other tools."
    elif "[force-revision]" in goal and not revised:
        verdict, score = "NEEDS_REVISION", 0.55
        reasons = ["The answer is incomplete."]
        feedback = "Add a knowledge-base search for more detail."
    else:
        verdict, score, feedback = "APPROVED", 0.92, ""
        reasons = ["All steps succeeded and the results address the goal."]
    return json.dumps(
        {"verdict": verdict, "score": score, "reasons": reasons, "feedback": feedback}
    )  # fmt: skip


def summary_handler(request: LLMRequest) -> str:
    """Extractive summary: the first words of each older message, capped in length."""
    text = _first_user_text(request)
    previous = (extract_tag("previous_summary", text) or "").strip()
    body = extract_tag("messages", text) or ""
    lines = [ln.strip() for ln in re.sub(r"<[^>]+>", "", body).splitlines() if ln.strip()]
    gist = " | ".join(ln[:40] for ln in lines)
    # WHY keep the TAIL when over the limit: the newest information matters most.
    return f"{previous} | {gist}".strip(" |")[-request.max_tokens * 4 :]


def register_mock_handlers(provider: LLMProvider) -> bool:
    """Teach the mock LLM about our tasks. No-op (False) for real providers."""
    if not isinstance(provider, MockProvider):
        return False
    provider.register("plan", plan_handler)
    provider.register("retrieve_answer", retrieval_handler)
    provider.register("review", review_handler)
    provider.register("plan_deep", plan_deep_handler)
    provider.register("followup_answer", followup_handler)
    provider.register("research_findings", research_handler)
    provider.register("synthesize_report", synthesize_handler)
    provider.register("summarize_conversation", summary_handler)
    return True
