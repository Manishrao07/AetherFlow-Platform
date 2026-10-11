"""Calculator must compute correctly and refuse everything that is not plain arithmetic."""

import math

import pytest

from app.tools.base import ToolError
from app.tools.builtin.calculator import evaluate


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("1 + 2 * 3", 7),
        ("(1200 * 0.18) + 50", 266.0),
        ("2 ** 10", 1024),
        ("-5 + +3", -2),
        ("10 // 3 + 10 % 3", 4),
        ("sqrt(16) + abs(-2)", 6.0),
        ("round(3.14159, 2)", 3.14),
        ("max(1, 5, 3) - min(4, 2)", 3),
        ("floor(2.7) + ceil(2.1)", 5),
        ("pi * 2", 2 * math.pi),
    ],
)
def test_valid_expressions(expression: str, expected: float) -> None:
    assert evaluate(expression) == pytest.approx(expected)


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('id')",
        "open('/etc/passwd')",
        "().__class__",
        "[1,2][0]",
        "'a' * 3",
        "x + 1",
        "lambda: 1",
        "True + 1",
        "eval('1')",
        "sqrt(x=4)",
        "1 +",
        "",
    ],
)
def test_non_arithmetic_is_rejected(expression: str) -> None:
    with pytest.raises(ToolError):
        evaluate(expression)


@pytest.mark.parametrize(
    "expression",
    ["9 ** 9 ** 9", "2 ** 5000", "10 ** 2000", "1 / 0", "5 % 0", "sqrt(-1)", "1e308 * 10"],
)
def test_dos_and_math_errors_are_controlled(expression: str) -> None:
    with pytest.raises(ToolError):
        evaluate(expression)


def test_expression_complexity_limit() -> None:
    with pytest.raises(ToolError, match="too complex"):
        evaluate("+".join(["1"] * 80))
