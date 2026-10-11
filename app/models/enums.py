"""Enumerations shared by models, the orchestrator and the API.

What: every closed set of values (statuses, roles, risk levels, ...).
Why: one definition used by the DB CHECK constraints, Pydantic schemas and the state
machine means they can never drift apart.
Convention: workflow/state enums are UPPERCASE (matches the spec's state diagrams); role
names and chat-message roles are lowercase (matches RBAC docs and LLM APIs).
"""

from __future__ import annotations

from enum import StrEnum


class RoleName(StrEnum):
    ADMIN = "admin"
    APPROVER = "approver"
    OPERATOR = "operator"
    VIEWER = "viewer"


class RunStatus(StrEnum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    REVIEWING = "REVIEWING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class AgentName(StrEnum):
    PLANNER = "planner"
    RETRIEVAL = "retrieval"
    EXECUTION = "execution"
    REVIEW = "review"
    # Deep-research mode: one researcher per sub-question, then one synthesizer for the report.
    RESEARCH = "research"
    SYNTHESIZER = "synthesizer"


class RunMode(StrEnum):
    """How a run is planned: ``STANDARD`` = tool plan, ``DEEP`` = sub-questions -> cited report."""

    STANDARD = "standard"
    DEEP = "deep"


class LLMCaller(StrEnum):
    """Who made an LLM call: the four agents plus the conversation-summary service.

    WHY separate from ``AgentName``: ``MEMORY`` calls are not plan steps, so a step can never
    be assigned to it, but the cost of summarising chats must still be attributed somewhere.
    """

    PLANNER = "planner"
    RETRIEVAL = "retrieval"
    EXECUTION = "execution"
    REVIEW = "review"
    RESEARCH = "research"
    SYNTHESIZER = "synthesizer"
    MEMORY = "memory"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ApprovalKind(StrEnum):
    PLAN = "PLAN"
    STEP = "STEP"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class ReviewVerdict(StrEnum):
    APPROVED = "APPROVED"
    NEEDS_REVISION = "NEEDS_REVISION"
    REJECTED = "REJECTED"


class ConversationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class MemoryKind(StrEnum):
    FACT = "FACT"
    PREFERENCE = "PREFERENCE"


class DocumentStatus(StrEnum):
    READY = "READY"
    FAILED = "FAILED"


class CallStatus(StrEnum):
    """Outcome of a single LLM call or tool execution attempt."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
