"""Idempotent demo data: one user per role plus sample knowledge-base documents.

What: ``seed_demo_data`` creates the demo users and ingests the sample documents. Running it
twice changes nothing the second time.
Why in ``app/`` and not only in ``scripts/``: the logic is unit-tested like any other
service; ``scripts/seed.py`` is just a thin command-line wrapper.
Connects to: ``scripts/seed.py``, integration tests, ``make seed``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import hash_password
from app.models import Role, User
from app.models.enums import RoleName
from app.services.documents import ingest_document

logger = logging.getLogger(__name__)

DEMO_USERS: tuple[tuple[str, str, RoleName], ...] = (
    ("admin@demo.local", "Alice Admin", RoleName.ADMIN),
    ("approver@demo.local", "Arjun Approver", RoleName.APPROVER),
    ("operator@demo.local", "Olivia Operator", RoleName.OPERATOR),
    ("viewer@demo.local", "Victor Viewer", RoleName.VIEWER),
)


@dataclass(frozen=True)
class SeedResult:
    users_created: int
    documents_created: int


async def _ensure_user(
    session: AsyncSession, email: str, full_name: str, role: RoleName, password: str
) -> bool:
    """Create the user (with its role) if missing. Returns True when created."""
    existing = await session.scalar(sa.select(User.id).where(sa.func.lower(User.email) == email))
    if existing is not None:
        return False
    role_row = await session.scalar(sa.select(Role).where(Role.name == role.value))
    if role_row is None:
        raise RuntimeError(f"role '{role.value}' missing - run `alembic upgrade head` first")
    # WHY the password is hashed here and never logged: only the argon2 hash is stored.
    session.add(
        User(
            email=email,
            full_name=full_name,
            password_hash=hash_password(password),
            roles=[role_row],
        )
    )
    return True


async def _ensure_document(session: AsyncSession, owner_id: uuid.UUID, path: Path) -> bool:
    """Ingest one markdown file through the normal ingestion path (idempotent by content)."""
    result = await ingest_document(
        session,
        owner_id,
        path.stem.replace("_", " ").title(),
        path.read_text(encoding="utf-8"),
        source=f"seed:{path.name}",
    )
    return result.created


async def seed_demo_data(
    session_factory: async_sessionmaker[AsyncSession], password: str, docs_dir: Path
) -> SeedResult:
    """Create missing demo users and sample documents in one transaction."""
    users_created = documents_created = 0
    async with session_factory() as session, session.begin():
        for email, full_name, role in DEMO_USERS:
            users_created += await _ensure_user(session, email, full_name, role, password)
        await session.flush()
        admin_id = await session.scalar(sa.select(User.id).where(User.email == "admin@demo.local"))
        if admin_id is None:
            raise RuntimeError("admin demo user missing after seeding users")
        for path in sorted(docs_dir.glob("*.md")):
            documents_created += await _ensure_document(session, admin_id, path)
    logger.info(
        "seed complete",
        extra={"users_created": users_created, "documents_created": documents_created},
    )
    return SeedResult(users_created, documents_created)
