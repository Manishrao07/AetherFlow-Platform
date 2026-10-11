"""send_notification: send an email (MOCKED - stored in the outbox table).

HIGH risk: it reaches people outside the system and cannot be undone, so the registry refuses
to run it until a human has approved the step. Idempotent per step like create_ticket.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.dialects.postgresql import insert

from app.models import NotificationOutbox
from app.models.enums import RiskLevel
from app.services.permissions import NOTIFICATION_SEND
from app.tools.base import Tool, ToolContext, ToolError


class SendNotificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient: str = Field(
        min_length=3,
        max_length=320,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
        examples=["ops@example.com"],
    )
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=4000)


class SendNotificationTool(Tool):
    name = "send_notification"
    description = "Send an email notification to a recipient (requires human approval)."
    input_model = SendNotificationInput
    risk = RiskLevel.HIGH
    permission = NOTIFICATION_SEND
    timeout_seconds = 10.0
    uses_breaker = True

    async def run(self, args: SendNotificationInput, ctx: ToolContext) -> dict[str, Any]:
        key = ctx.side_effect_key or ctx.idempotency_key
        if not key:
            raise ToolError("idempotency key missing")
        async with ctx.session_factory() as session, session.begin():
            inserted = await session.scalar(
                insert(NotificationOutbox)
                .values(
                    user_id=ctx.principal.user_id,
                    run_id=ctx.run_id,
                    recipient=args.recipient,
                    subject=args.subject,
                    body=args.body,
                    idempotency_key=f"notify:{key}",
                )
                .on_conflict_do_nothing(constraint="uq_notification_outbox_idempotency_key")
                .returning(NotificationOutbox.id)
            )
            notification_id = inserted or await session.scalar(
                sa.select(NotificationOutbox.id).where(
                    NotificationOutbox.idempotency_key == f"notify:{key}"
                )
            )
        return {
            "notification_id": str(notification_id),
            "channel": "email",
            "recipient": args.recipient,
            "status": "sent (mock)",
            "created": inserted is not None,
        }
