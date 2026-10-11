"""Tool catalogue endpoint."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from app.api.deps import require_permission_dep
from app.core.rbac import Principal
from app.services.permissions import PERMISSIONS, WORKFLOW_READ

router = APIRouter(prefix="/api/v1/tools", tags=["tools"])

ReaderDep = Annotated[Principal, Depends(require_permission_dep(WORKFLOW_READ))]


@router.get("", summary="List available tools")
async def list_tools(request: Request, principal: ReaderDep) -> dict[str, Any]:
    """Every registered tool with its schema, risk level and whether YOUR role may use it."""
    specs = request.app.state.tool_registry.specs()
    for spec in specs:
        spec["allowed_for_you"] = PERMISSIONS.allows(principal.roles, spec["permission"])
    return {"items": specs, "total": len(specs)}
