"""ORM models. Importing this package registers every table on ``Base.metadata``.

WHY the explicit imports: Alembic autogenerate and ``create_all`` only know about models
that have been imported.
"""

from app.models.approval import Approval
from app.models.audit import AuditLog
from app.models.auth import RefreshToken
from app.models.conversation import Conversation, Message
from app.models.integration import NotificationOutbox, Ticket
from app.models.knowledge import Document, DocumentChunk, Memory
from app.models.user import Role, User, UserRole
from app.models.workflow import (
    LLMCall,
    RunEvent,
    StepDependency,
    ToolExecution,
    WorkflowRun,
    WorkflowStep,
)

__all__ = [
    "Approval",
    "AuditLog",
    "Conversation",
    "Document",
    "DocumentChunk",
    "LLMCall",
    "Memory",
    "Message",
    "NotificationOutbox",
    "RefreshToken",
    "Role",
    "RunEvent",
    "StepDependency",
    "Ticket",
    "ToolExecution",
    "User",
    "UserRole",
    "WorkflowRun",
    "WorkflowStep",
]
