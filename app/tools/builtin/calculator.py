"""calculator: safe arithmetic evaluation.

Why not ``eval``: ``eval`` on model/user text is arbitrary code execution. We parse to an AST
and only allow whitelisted node types, operators and functions, with size limits so
"9**9**9" cannot freeze the server (denial of service).
"""

from __future__ import annotations

import ast
import math
import operator
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import RiskLevel
from app.services.permissions import WORKFLOW_CREATE
from app.tools.base import Tool, ToolContext, ToolError

_BINARY: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "floor": math.floor,
    "ceil": math.ceil,
}
_CONSTANTS = {"pi": math.pi, "e": math.e}
_MAX_NODES = 100
_MAX_EXPONENT = 1000
_MAX_INT_BITS = 4096


def evaluate(expression: str) -> float | int:
    """Evaluate an arithmetic expression or raise ``ToolError`` (never executes code)."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ToolError("not a valid arithmetic expression") from exc
    if sum(1 for _ in ast.walk(tree)) > _MAX_NODES:
        raise ToolError("expression is too complex")
    try:
        result = _eval(tree.body)
    except ZeroDivisionError as exc:
        raise ToolError("division by zero") from exc
    except OverflowError as exc:
        raise ToolError("result is too large") from exc
    except (ValueError, TypeError) as exc:
        raise ToolError(f"cannot evaluate: {exc}") from exc
    if isinstance(result, float) and not math.isfinite(result):
        raise ToolError("result is not a finite number")
    return result  # type: ignore[no-any-return]


def _eval(node: ast.AST) -> Any:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    ):
        return node.value
    if isinstance(node, ast.Name) and node.id in _CONSTANTS:
        return _CONSTANTS[node.id]
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow):
            _check_power(left, right)
        return _BINARY[type(node.op)](left, right)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _FUNCTIONS
        and not node.keywords
    ):
        return _FUNCTIONS[node.func.id](*(_eval(a) for a in node.args))
    raise ToolError("only numbers, + - * / // % **, parentheses and basic functions are allowed")


def _check_power(base: Any, exponent: Any) -> None:
    if abs(exponent) > _MAX_EXPONENT:
        raise ToolError("exponent is too large")
    if (
        isinstance(base, int)
        and isinstance(exponent, int)
        and exponent > 0
        and base.bit_length() * exponent > _MAX_INT_BITS
    ):
        raise ToolError("result is too large")


class CalculatorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expression: str = Field(min_length=1, max_length=200, examples=["(1200 * 0.18) + 50"])


class CalculatorTool(Tool):
    name = "calculator"
    description = "Evaluate an arithmetic expression, e.g. '(1200 * 0.18) + 50'."
    input_model = CalculatorInput
    risk = RiskLevel.LOW
    permission = WORKFLOW_CREATE
    timeout_seconds = 2.0

    async def run(self, args: CalculatorInput, ctx: ToolContext) -> dict[str, Any]:
        return {"expression": args.expression, "result": evaluate(args.expression)}
