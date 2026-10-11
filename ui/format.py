"""Pure helpers that turn API data into display text and tables (no Streamlit calls here).

Why separate: formatting rules (status colours, cost rounding, tables) are easy to unit-test when
they do not depend on the Streamlit runtime, and every screen shares the same look this way.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any

# One place decides what each state looks like. Names are Streamlit's badge colours.
STATUS_COLORS = {
    "CREATED": "gray", "PLANNING": "blue", "AWAITING_APPROVAL": "orange", "EXECUTING": "blue",
    "REVIEWING": "violet", "COMPLETED": "green", "FAILED": "red", "CANCELLED": "gray",
    "PENDING": "gray", "RUNNING": "blue", "SUCCEEDED": "green", "SKIPPED": "gray",
    "APPROVED": "green", "REJECTED": "red", "EXPIRED": "gray", "NEEDS_REVISION": "orange",
    "TIMEOUT": "red", "LOW": "green", "MEDIUM": "orange", "HIGH": "red",
}  # fmt: skip
STATUS_ICONS = {
    "COMPLETED": "✅", "SUCCEEDED": "✅", "APPROVED": "✅", "FAILED": "❌", "REJECTED": "❌",
    "CANCELLED": "⏹️", "SKIPPED": "⏭️", "AWAITING_APPROVAL": "⏸️", "RUNNING": "⏳",
    "EXECUTING": "⏳", "PLANNING": "🧭", "REVIEWING": "🔎", "PENDING": "·", "CREATED": "·",
    "NEEDS_REVISION": "🔁", "EXPIRED": "⌛",
}  # fmt: skip
TERMINAL_RUN_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
# Where waiting is pointless because a human (not the system) has to act.
PAUSED_RUN_STATUSES = frozenset({"AWAITING_APPROVAL"})


_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#!|<>~$:@&])")
_LIST_OR_RULE_START = re.compile(r"(?m)^(\s*)(\d+)([.)])|^(\s*)([-+=])")


def plain(value: Any) -> str:
    """Escape text so Streamlit shows it literally instead of interpreting it as Markdown.

    WHY: goals, documents and model answers are untrusted. Rendered as Markdown they could show
    misleading links or load remote images, and an image URL is a well-known way for a prompt
    injection to leak data. Escaping makes every character display as typed (emails are not
    auto-linked either); line breaks are kept.
    """
    text = "" if value is None else str(value).replace("\r\n", "\n")
    text = _MARKDOWN_SPECIAL.sub(r"\\\1", text)
    # WHY a zero-width space: the renderer turns bare "http://..." and "www...." into clickable
    # links after escapes are processed. Breaking the pattern invisibly keeps URLs readable
    # (and still copyable) but not one click away.
    text = text.replace("//", "/\u200b/").replace("www.", "www\u200b.")
    # Same for emails: the renderer auto-links "name@host" even when the "@" is escaped.
    text = text.replace("\\@", "\\@\u200b")
    # Only at the start of a line do "1.", "-", "+" and "=" mean something (lists, headings).
    text = _LIST_OR_RULE_START.sub(
        lambda m: f"{m[1]}{m[2]}\\{m[3]}" if m[2] else f"{m[4]}\\{m[5]}", text
    )
    return text.replace("\n", "  \n")


_BLOCK_PREFIX = re.compile(r"^(#{1,3} |\s*[-*] |\s*\d+\. )")


def report_markdown(value: Any) -> str:
    """Render a research report: headings, lists and tables work; everything else is literal.

    WHY not plain Markdown: the report is built from web pages (untrusted), so images, links and
    HTML in it must never become live. Structure characters are kept only where WE expect them
    (a heading, list marker or table row at the start of a line); all text after them goes
    through ``plain``.
    """
    lines: list[str] = []
    for raw in ("" if value is None else str(value)).replace("\r\n", "\n").split("\n"):
        if raw.lstrip().startswith("|"):  # a table row: keep the pipes, escape each cell
            cells = [
                plain(c.strip()).replace("  \n", " ") for c in raw.strip().strip("|").split("|")
            ]
            lines.append("| " + " | ".join(cells) + " |")
        elif match := _BLOCK_PREFIX.match(raw):
            prefix = match.group(1)
            lines.append(prefix + plain(raw[len(prefix) :]).replace("  \n", " "))
        else:
            lines.append(plain(raw).replace("  \n", " "))
    return "\n".join(lines)


def _code(value: Any) -> str:
    """Text for inside a Markdown code span, where backslash escapes do not work."""
    return str(value).replace("`", "'")


def badge(value: str | None) -> str:
    """Markdown for a coloured badge (``st.markdown`` renders it)."""
    text = value or "-"
    return f":{STATUS_COLORS.get(text, 'gray')}-badge[{text}]"


def label(value: str | None) -> str:
    """Plain text with an icon, for table cells (tables cannot render badges)."""
    text = value or "-"
    return f"{STATUS_ICONS.get(text, '·')} {text}"


def is_final(status: str) -> bool:
    return status in TERMINAL_RUN_STATUSES


def money(value: Any) -> str:
    """USD with enough digits for sub-cent LLM costs."""
    amount = Decimal(str(value or 0))
    return f"${amount:.4f}" if amount < 1 else f"${amount:,.2f}"


def short_time(value: str | None) -> str:
    if not value:
        return "-"
    return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")


def duration(start: str | None, end: str | None) -> str:
    if not start or not end:
        return "-"
    delta = datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(
        start.replace("Z", "+00:00")
    )
    seconds = delta.total_seconds()
    return f"{seconds * 1000:.0f} ms" if seconds < 1 else f"{seconds:.1f} s"


def steps_table(
    steps: Iterable[dict[str, Any]], revision: int | None = None
) -> list[dict[str, Any]]:
    """Rows for the plan table (one revision at a time)."""
    return [
        {
            "step": s["step_key"],
            "agent": s["agent"],
            "tool": s["tool"] or "-",
            "status": label(s["status"]),
            "risk": s["risk_level"],
            "needs approval": "yes" if s["requires_approval"] else "",
            "attempts": f"{s['attempts']}/{s['max_attempts']}",
            "time": duration(s["started_at"], s["finished_at"]),
        }
        for s in sorted(steps, key=lambda s: s["position"])
        if revision is None or s["revision"] == revision
    ]


def _short(value: Any, limit: int = 140) -> str:
    """One line of an error for a table cell (cells show plain text, not Markdown)."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def llm_table(calls: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "agent": c["agent"],
            "model": c["model"],
            "status": label(c["status"]),
            "tokens in": c["prompt_tokens"],
            "tokens out": c["completion_tokens"],
            "latency": f"{c['latency_ms']} ms",
            "cost": money(c["cost_usd"]),
            "cached": "yes" if c["cached"] else "",
            "error": _short(c.get("error")),
        }
        for c in calls
    ]


def tool_table(
    executions: Iterable[dict[str, Any]], step_keys: dict[str, str]
) -> list[dict[str, Any]]:
    return [
        {
            "step": step_keys.get(t["step_id"], "?"),
            "tool": t["tool_name"],
            "attempt": t["attempt"],
            "status": label(t["status"]),
            "duration": f"{t['duration_ms']} ms" if t["duration_ms"] is not None else "-",
            "error": t["error"] or "",
        }
        for t in executions
    ]


def event_line(event: dict[str, Any]) -> str:
    """One readable line per timeline event."""
    data, kind = event["data"], event["type"]
    when = short_time(event["created_at"])[11:]
    match kind:
        case "run.status":
            text = f"run {plain(data.get('from'))} → **{plain(data.get('to'))}**" + (
                f" ({plain(data['reason'])})" if data.get("reason") else ""
            )
        case "step.status":
            text = f"step `{_code(data.get('key', '?'))}` → **{plain(data.get('to'))}**"
        case "step.retry":
            text = (
                f"step `{_code(data.get('key'))}` attempt {data.get('attempt')} failed, "
                f"retrying: {plain(data.get('error'))}"
            )
        case "review.verdict":
            text = f"review: **{plain(data.get('verdict'))}** (score {data.get('score')})"
        case "approval.decided":
            text = (
                f"approval {'approved' if data.get('approved') else 'rejected'} "
                f"by {plain(data.get('by'))}"
            )
        case _:
            text = plain(kind)
    return f"`{when}`  {text}"


def parse_prometheus(text: str) -> dict[str, list[tuple[dict[str, str], float]]]:
    """``/metrics`` text -> {sample name: [(labels, value), ...]} (only what the screen needs)."""
    from prometheus_client.parser import text_string_to_metric_families

    samples: dict[str, list[tuple[dict[str, str], float]]] = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            samples.setdefault(sample.name, []).append((dict(sample.labels), sample.value))
    return samples


def total(samples: dict[str, list[tuple[dict[str, str], float]]], name: str, **where: str) -> float:
    """Sum of the samples called ``name`` whose labels match ``where``."""
    return sum(
        value
        for labels, value in samples.get(name, [])
        if all(labels.get(k) == v for k, v in where.items())
    )
