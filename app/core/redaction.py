"""Secret and PII redaction for logs and stored traces.

What: ``redact()`` walks any JSON-like value and masks (a) values stored under sensitive
keys such as ``password`` or ``api_key`` and (b) secret-looking or personal strings
(bearer tokens, JWTs, API keys, emails, card-like numbers).
Why: tool inputs/outputs and LLM prompts are persisted for the execution timeline; without
redaction a user pasting a password or token would be stored (and shown) forever.
Connects to: ``logging.py`` (every log record), and later the tool executor / trace writer.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

# WHY substring match: "db_password", "X-Api-Key", "refreshToken" should all be caught.
_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
)

# Order matters little; each pattern replaces its own match with a labelled marker.
_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)bearer\s+[a-z0-9._\-]+"), "Bearer [REDACTED]"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "[JWT]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "[API_KEY]"),
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    (re.compile(r"\b\d(?:[ -]?\d){12,18}\b"), "[CARD_NUMBER]"),
)


# WHY an allow-list of COUNT names: "prompt_tokens" is a number of LLM tokens, not a credential,
# but it contains the word "token". Masking it blanked token usage in logs and trace attributes.
# Only the exact counter names are exempt, so "access_token", "refresh_token" or "api_token"
# are still masked.
_TOKEN_COUNT_KEYS = re.compile(
    r"^(prompt|completion|input|output|total|max|cached|reasoning)_tokens$|^token_count$"
)


def _is_sensitive_key(key: str) -> bool:
    # WHY normalise '-': HTTP headers use 'X-Api-Key' while code uses 'api_key'.
    lowered = key.lower().replace("-", "_")
    if _TOKEN_COUNT_KEYS.match(lowered):
        return False
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def redact_text(text: str) -> str:
    """Mask secret-looking or personal substrings inside a free-text string."""
    for pattern, replacement in _VALUE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact(value: Any) -> Any:
    """Return a redacted deep copy of a JSON-like value (dict / list / str / scalar)."""
    if isinstance(value, dict):
        return {
            k: REDACTED if isinstance(k, str) and _is_sensitive_key(k) else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
