"""Unit tests for the pure display helpers in ``ui.format``."""

from __future__ import annotations

from typing import Any

from ui import format as fmt


def test_badge_and_label_fall_back_for_unknown_or_missing() -> None:
    assert fmt.badge("COMPLETED") == ":green-badge[COMPLETED]"
    assert fmt.badge("WEIRD") == ":gray-badge[WEIRD]"
    assert fmt.badge(None) == ":gray-badge[-]"
    assert fmt.label("FAILED") == "❌ FAILED"
    assert fmt.label("WEIRD") == "· WEIRD"
    assert fmt.label(None) == "· -"


def test_is_final_only_for_terminal_states() -> None:
    assert all(fmt.is_final(s) for s in ("COMPLETED", "FAILED", "CANCELLED"))
    assert not any(fmt.is_final(s) for s in ("EXECUTING", "AWAITING_APPROVAL", "CREATED"))


def test_money_keeps_sub_cent_precision() -> None:
    assert fmt.money(None) == "$0.0000"
    assert fmt.money("0.00123") == "$0.0012"
    assert fmt.money(1234.5) == "$1,234.50"


def test_time_helpers() -> None:
    assert fmt.short_time(None) == "-"
    assert fmt.short_time("2026-01-02T03:04:05Z") == "2026-01-02 03:04:05"
    assert fmt.duration(None, "x") == "-"
    assert fmt.duration("2026-01-01T00:00:00Z", "2026-01-01T00:00:00.250Z") == "250 ms"
    assert fmt.duration("2026-01-01T00:00:00Z", "2026-01-01T00:00:02.5Z") == "2.5 s"


def _step(key: str, position: int, revision: int = 0, **extra: Any) -> dict[str, Any]:
    base = {
        "step_key": key, "agent": "execution", "tool": None, "status": "SUCCEEDED",
        "risk_level": "LOW", "requires_approval": False, "attempts": 1, "max_attempts": 3,
        "started_at": None, "finished_at": None, "position": position, "revision": revision,
    }  # fmt: skip
    return base | extra


def test_steps_table_sorts_filters_revision_and_formats() -> None:
    rows = fmt.steps_table(
        [_step("b", 2, tool="calculator", requires_approval=True), _step("a", 1), _step("z", 1, 1)],
        revision=0,
    )
    assert [r["step"] for r in rows] == ["a", "b"]
    assert rows[1]["tool"] == "calculator"
    assert rows[1]["needs approval"] == "yes"
    assert rows[0]["needs approval"] == ""
    assert rows[0]["attempts"] == "1/3"
    assert len(fmt.steps_table([_step("a", 1), _step("z", 1, 1)])) == 2


def test_llm_and_tool_tables() -> None:
    llm = fmt.llm_table(
        [
            {
                "agent": "planner", "model": "m", "status": "SUCCEEDED", "prompt_tokens": 5,
                "completion_tokens": 7, "latency_ms": 12, "cost_usd": "0.5", "cached": True,
            }
        ]
    )  # fmt: skip
    assert llm[0]["cost"] == "$0.5000"
    assert llm[0]["cached"] == "yes"
    tools = fmt.tool_table(
        [
            {"step_id": "s1", "tool_name": "t", "attempt": 1, "status": "FAILED",
             "duration_ms": None, "error": "boom"},
            {"step_id": "zz", "tool_name": "t", "attempt": 2, "status": "SUCCEEDED",
             "duration_ms": 9, "error": None},
        ],
        {"s1": "first"},
    )  # fmt: skip
    assert tools[0]["step"] == "first"
    assert tools[0]["duration"] == "-"
    assert tools[0]["error"] == "boom"
    assert tools[1]["step"] == "?"
    assert tools[1]["duration"] == "9 ms"
    assert tools[1]["error"] == ""


def _event(kind: str, **data: Any) -> dict[str, Any]:
    return {"type": kind, "data": data, "created_at": "2026-01-01T10:20:30Z"}


def test_event_line_covers_every_event_kind() -> None:
    assert "`10:20:30`" in fmt.event_line(_event("run.created"))
    assert "PLANNING → **EXECUTING** (go)" in fmt.event_line(
        _event("run.status", **{"from": "PLANNING", "to": "EXECUTING", "reason": "go"})
    )
    assert "(" not in fmt.event_line(_event("run.status", **{"from": "A", "to": "B"}))
    assert "step `s` → **FAILED**" in fmt.event_line(_event("step.status", key="s", to="FAILED"))
    assert "attempt 2 failed, retrying: x" in fmt.event_line(
        _event("step.retry", key="s", attempt=2, error="x")
    )
    assert "**approve** (score 0.9)" in fmt.event_line(
        _event("review.verdict", verdict="approve", score=0.9)
    )
    assert "approved by bob" in fmt.event_line(_event("approval.decided", approved=True, by="bob"))
    assert "rejected by bob" in fmt.event_line(_event("approval.decided", approved=False, by="bob"))
    assert fmt.event_line(_event("custom.thing")).endswith("custom.thing")


def test_parse_prometheus_and_total() -> None:
    text = (
        "# TYPE jobs_total counter\n"
        'jobs_total{kind="a",status="ok"} 3\n'
        'jobs_total{kind="b",status="ok"} 4\n'
        'jobs_total{kind="b",status="err"} 1\n'
    )
    samples = fmt.parse_prometheus(text)
    assert fmt.total(samples, "jobs_total") == 8
    assert fmt.total(samples, "jobs_total", status="ok") == 7
    assert fmt.total(samples, "jobs_total", kind="b", status="err") == 1
    assert fmt.total(samples, "missing") == 0
    assert fmt.parse_prometheus("") == {}


def test_plain_escapes_markdown_so_untrusted_text_cannot_render() -> None:
    hostile = (
        "[click](http://evil.example) ![](http://evil.example/p.png?d=secret) "
        "<b>x</b> :red-badge[x]"
    )
    escaped = fmt.plain(hostile)
    assert "](" not in escaped.replace("\\]\\(", "")  # every link bracket is escaped
    assert "\\[click\\]\\(http" in escaped
    assert "\\!\\[" in escaped  # images are neutralised
    assert "\\<b\\>" in escaped  # no HTML
    assert "\\:red" in escaped  # no badge/emoji directives
    assert fmt.plain("ops@example.com") == "ops\\@\u200bexample.com"  # not auto-linked
    assert fmt.plain(None) == ""
    assert fmt.plain("a\r\nb\nc") == "a  \nb  \nc"  # line breaks survive


def test_event_line_neutralises_hostile_fields_and_backticks() -> None:
    line = fmt.event_line(
        _event("step.retry", key="a`b", attempt=1, error="[x](http://evil.example)")
    )
    assert "a'b" in line
    assert "\\[x\\]\\(http" in line


def test_plain_escapes_line_start_markers_only() -> None:
    assert fmt.plain("1. first\n- item\n+ more\n=== title") == (
        "1\\. first  \n\\- item  \n\\+ more  \n\\=== title"
    )
    assert fmt.plain("a-b 2.5 x+y") == "a-b 2.5 x+y"  # inside a line these are harmless


def test_plain_defuses_bare_urls_without_changing_how_they_read() -> None:
    shown = fmt.plain("see http://evil.example/x and www.evil.example")
    assert "//" not in shown.replace("/\u200b/", "")
    assert (
        shown.replace("\u200b", "").replace("\\", "")
        == "see http://evil.example/x and www.evil.example"
    )


def test_report_markdown_keeps_structure_but_neutralises_content() -> None:
    text = (
        "# Title\n\n## Part [1]\n- point with ![x](http://evil/p.png)\n"
        "1. Source - http://example.com\n\n| A | B |\n|---|---|\n| <b>x</b> | y |\n"
    )
    out = fmt.report_markdown(text)
    lines = out.splitlines()
    assert lines[0] == "# Title" and lines[2].startswith("## Part ")
    assert "\\[1\\]" in lines[2]  # citation markers are literal text, not links
    assert lines[3].startswith("- point with \\!\\[x\\]")
    assert lines[4].startswith("1. Source") and "example.com" in lines[4]
    assert "http://" not in lines[4]  # the URL is not a live link
    assert lines[6] == "| A | B |" and "\\<b\\>x" in lines[8]
    assert fmt.report_markdown(None) == ""


def test_llm_table_shows_a_short_one_line_error() -> None:
    call = {
        "agent": "research", "model": "m", "status": "FAILED", "prompt_tokens": 0,
        "completion_tokens": 0, "latency_ms": 5, "cost_usd": "0", "cached": False,
        "error": "LLMRateLimitError: provider rate limit:\n" + "x" * 300,
    }  # fmt: skip
    cell = fmt.llm_table([call, call | {"error": None}])[0]["error"]
    assert "\n" not in cell and len(cell) <= 140 and cell.endswith("…")
    assert cell.startswith("LLMRateLimitError: provider rate limit:")
    assert fmt.llm_table([call | {"error": None}])[0]["error"] == ""
