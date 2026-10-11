"""create_ticket: open a ticket in the (mocked) external ticketing system.

Idempotent: keyed by the STEP, so retries or a replay after a crash return the same ticket
instead of creating duplicates (INSERT ... ON CONFLICT on the unique idempotency_key).
"""

from __future__ import annotations

from typing import Any, Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.dialects.postgresql import insert

from app.models import Ticket
from app.models.enums import RiskLevel
from app.services.permissions import TICKET_CREATE
from app.tools.base import Tool, ToolContext, ToolError


class CreateTicketInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    severity: Literal["SEV1", "SEV2", "SEV3", "SEV4"] = "SEV3"


class CreateTicketTool(Tool):
    name = "create_ticket"
    description = "Create a support/incident ticket (SEV1 most urgent to SEV4)."
    input_model = CreateTicketInput
    risk = RiskLevel.MEDIUM
    permission = TICKET_CREATE
    timeout_seconds = 10.0

    async def run(self, args: CreateTicketInput, ctx: ToolContext) -> dict[str, Any]:
        key = ctx.side_effect_key or ctx.idempotency_key
        if not key:
            raise ToolError("idempotency key missing")
        async with ctx.session_factory() as session, session.begin():
            inserted = await session.scalar(
                insert(Ticket)
                .values(
                    user_id=ctx.principal.user_id,
                    run_id=ctx.run_id,
                    title=args.title,
                    description=args.description,
                    severity=args.severity,
                    idempotency_key=f"ticket:{key}",
                )
                .on_conflict_do_nothing(constraint="uq_tickets_idempotency_key")
                .returning(Ticket.id)
            )
            ticket_id = inserted or await session.scalar(
                sa.select(Ticket.id).where(Ticket.idempotency_key == f"ticket:{key}")
            )
        return {
            "ticket_id": str(ticket_id),
            "severity": args.severity,
            "status": "OPEN",
            "created": inserted is not None,
        }
