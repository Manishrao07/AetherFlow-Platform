"""Query expansion: a small, explicit synonym list for keyword search.

What: ``expand_query`` appends extra words to a question when it contains a known phrase, so
"money back" also searches for "refund".
Why: Postgres full-text search matches words, not meaning. Without this, "How long to get my
money back?" finds nothing in a policy that only says "refund". A hand-written list is
deterministic, works offline with the mock LLM, and is trivial to explain; it only knows the
pairs written here. The real fix for open-ended wording is embeddings (pgvector), which is on
the roadmap (docs/tradeoffs.md).
How it connects: ``search_chunks`` expands the query before ranking, and the mock retrieval
handler expands the question the same way when picking the best sentence, so both agree.
"""

from __future__ import annotations

import re

# WHY phrase -> extra words (not a replacement): the original wording still counts, the
# extra words only widen what can match, and the OR-ranking keeps best matches first.
SYNONYMS: dict[str, str] = {
    "money back": "refund issued",
    "get my money": "refund issued",
    "reimburse": "refund",
    "reimbursement": "refund",
    "how long": "within days hours",
    "how often": "every",
    "maximum": "capped limit",
    "limit": "capped",
    "cost": "dollars",
    "price": "dollars",
    "reply": "response",
    "respond": "response",
    "outage": "incident",
    "escalate": "escalation",
    "stolen": "lost",
    "deadline": "within",
}


def expand_query(query: str) -> str:
    """Return ``query`` plus the synonyms of every known phrase it contains (original first)."""
    lowered = " ".join(re.findall(r"[a-z0-9]+", query.lower()))
    extras = [
        words
        for phrase, words in SYNONYMS.items()
        if re.search(rf"\b{re.escape(phrase)}\b", lowered)
    ]
    return " ".join([query, *extras]) if extras else query
