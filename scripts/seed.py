"""Command-line entry point: ``python scripts/seed.py`` (or ``make seed``).

Creates one demo user per role and loads sample documents. Safe to run repeatedly.
Refuses to run in production so demo credentials can never reach a real deployment.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from app.core.config import get_settings
from app.core.db.session import build_engine, build_session_factory, wait_for_db
from app.core.logging import configure_logging
from app.services.seeding import DEMO_USERS, seed_demo_data

DOCS_DIR = Path(__file__).parent / "sample_docs"


async def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=False)
    if settings.is_production:
        print("Refusing to seed demo data when ENVIRONMENT=production", file=sys.stderr)
        return 1
    password = os.environ.get("SEED_DEMO_PASSWORD", "Demo#Pass123")
    engine = build_engine(settings)
    try:
        await wait_for_db(
            engine, settings.db_connect_retries, settings.db_connect_retry_delay_seconds
        )
        result = await seed_demo_data(build_session_factory(engine), password, DOCS_DIR)
    finally:
        await engine.dispose()
    print(f"Seed done: {result.users_created} users, {result.documents_created} documents created.")
    print("Demo logins (password from SEED_DEMO_PASSWORD, default 'Demo#Pass123'):")
    for email, _, role in DEMO_USERS:
        print(f"  {role.value:<9} {email}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
