"""``{{step_key}}`` placeholders: let a step use the output of an earlier step.

What: find placeholders in a step's inputs, and replace them with a short TEXT rendering of
the referenced step's output.
Why: a plan like "search the refund policy, then open a ticket summarising it" needs data to
flow from step one into step two. Doing it with plain text substitution (no code evaluation)
keeps it safe: the substituted text is only ever DATA inside a tool's already-validated input
field, never an instruction to an LLM, and it is length-capped.
Connects to: ``plan.py`` (declares/validates dependencies), ``execution.py`` (renders inputs).
"""

from __future__ import annotations

import json
import re
from typing import Any

PLACEHOLDER = re.compile(r"\{\{\s*([a-z][a-z0-9_]{0,31})\s*\}\}")
MAX_RENDERED_CHARS = 1500


def placeholders_in(value: Any) -> set[str]:
    """All step keys referenced anywhere inside ``value`` (strings, lists, dicts)."""
    if isinstance(value, str):
        return set(PLACEHOLDER.findall(value))
    if isinstance(value, dict):
        return set().union(*(placeholders_in(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(placeholders_in(v) for v in value)) if value else set()
    return set()


def render_output(output: dict[str, Any]) -> str:
    """Turn a step output into compact text (retrieval results become cited snippets)."""
    results = output.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        lines = [f"[{r.get('citation', '?')}] {str(r.get('content', ''))[:300]}" for r in results]
        text = "\n".join(lines)
    elif isinstance(output.get("answer"), str):
        text = str(output["answer"])
    else:
        text = json.dumps(output, default=str, sort_keys=True)
    return text[:MAX_RENDERED_CHARS]


def substitute(value: Any, outputs: dict[str, dict[str, Any]], *, dummy: bool = False) -> Any:
    """Return ``value`` with every placeholder replaced.

    ``dummy=True`` substitutes "x" so a plan can be schema-checked before anything has run.
    A reference to a step with no output yet raises ``KeyError`` (the scheduler guarantees
    dependencies finish first, so this signals a bug rather than user error).
    """
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            return "x" if dummy else render_output(outputs[match.group(1)])

        return PLACEHOLDER.sub(replace, value)
    if isinstance(value, dict):
        return {k: substitute(v, outputs, dummy=dummy) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, outputs, dummy=dummy) for v in value]
    return value
