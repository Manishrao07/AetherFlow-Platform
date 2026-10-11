"""Schemas for authentication and user management."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.models.enums import RoleName
from app.models.user import User

# WHY a simple pattern instead of EmailStr: EmailStr rejects reserved domains such as
# ".local" which our demo accounts use; real deliverability is not our concern here.
Email = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        max_length=320,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    ),
]


class RegisterRequest(BaseModel):
    email: Email
    full_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=10, max_length=128)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "email": "jane@example.com",
                "full_name": "Jane Doe",
                "password": "correct-horse-9",
            }
        }
    )

    @field_validator("password")
    @classmethod
    def _password_policy(cls, value: str) -> str:
        """Length >= 10 plus at least one letter and one digit (blocks the laziest choices)."""
        if not re.search(r"[A-Za-z]", value) or not re.search(r"\d", value):
            raise ValueError("password must contain at least one letter and one digit")
        return value


class LoginRequest(BaseModel):
    email: Email
    password: str = Field(min_length=1, max_length=128)

    model_config = ConfigDict(
        json_schema_extra={"example": {"email": "operator@demo.local", "password": "Demo#Pass123"}}
    )


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=20, max_length=2048)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105  (OAuth2 token *type*, not a secret)
    expires_in: int = Field(description="Access token lifetime in seconds")


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    roles: list[str]
    is_active: bool
    created_at: datetime

    @classmethod
    def from_user(cls, user: User) -> UserOut:
        return cls(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            roles=sorted(role.name for role in user.roles),
            is_active=user.is_active,
            created_at=user.created_at,
        )


class RolesUpdate(BaseModel):
    roles: list[RoleName] = Field(min_length=1, description="Replaces the user's roles")

    model_config = ConfigDict(json_schema_extra={"example": {"roles": ["operator", "approver"]}})


class ActiveUpdate(BaseModel):
    is_active: bool


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    actor_id: uuid.UUID | None
    actor_email: str | None
    action: str
    resource_type: str | None
    resource_id: str | None
    outcome: str
    details: dict[str, object]
    ip_address: str | None
    request_id: str | None
    created_at: datetime

    @field_validator("ip_address", mode="before")
    @classmethod
    def _ip_to_str(cls, value: object) -> str | None:
        return None if value is None else str(value)
