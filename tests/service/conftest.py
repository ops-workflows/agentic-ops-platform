"""Layer 1 pytest configuration.

Design note on event-loop scoping:

pytest-asyncio creates a fresh event loop for every test function unless
told otherwise. Session-scoped async fixtures that hold open asyncpg
connections break on the next function because those connections are
tied to the loop that was open at fixture creation time.

To avoid that entire class of bugs we:

- initialize the Postgres schema exactly once per pytest session via a
  synchronous pre-check (using a one-shot asyncpg connection on a
  throwaway loop)
- build the SQLAlchemy async engine fresh per test function
- TRUNCATE all tables before each test for isolation
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_SQL = REPO_ROOT / "shared" / "lib" / "init_db.sql"


def _async_dsn(raw: str) -> str:
    if raw.startswith("postgres://"):
        raw = "postgresql+asyncpg://" + raw.removeprefix("postgres://")
    elif raw.startswith("postgresql://"):
        raw = "postgresql+asyncpg://" + raw.removeprefix("postgresql://")
    return raw


def _raw_dsn(async_dsn: str) -> str:
    return async_dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


_schema_initialized = False


def _assert_safe_test_dsn(dsn: str) -> None:
    """Refuse to drop schemas in any DB that isn't clearly a test DB.

    The fixture below DROPs ``control_plane`` and ``task_queue`` schemas.
    Running it against a developer's local Postgres (e.g. ``agentic_ops``)
    silently destroys real data. We require either an explicit opt-in
    (``TEST_ALLOW_DB_WIPE=1``) or a database name that ends in ``_test``.
    """
    lowered = dsn.lower()
    if os.environ.get("TEST_ALLOW_DB_WIPE") == "1":
        return
    # crude DB-name extraction: substring after the last '/' up to '?'
    db_name = lowered.rsplit("/", 1)[-1].split("?", 1)[0]
    if db_name.endswith("_test") or db_name.startswith("test_"):
        return
    raise RuntimeError(
        "Refusing to run service tests against TEST_DATABASE_URL="
        f"{dsn!r}: the database name {db_name!r} does not look like a "
        "test database. The suite DROPs control_plane + task_queue "
        "schemas before each run. Either point TEST_DATABASE_URL at a "
        "dedicated test DB (suffix '_test' or prefix 'test_') or set "
        "TEST_ALLOW_DB_WIPE=1 to override."
    )


def _ensure_schema_once(dsn: str) -> None:
    global _schema_initialized
    if _schema_initialized:
        return

    _assert_safe_test_dsn(dsn)

    import asyncpg

    async def _go() -> None:
        conn = await asyncpg.connect(_raw_dsn(_async_dsn(dsn)))
        try:
            await conn.execute("DROP SCHEMA IF EXISTS control_plane CASCADE")
            await conn.execute("DROP SCHEMA IF EXISTS task_queue CASCADE")
            await conn.execute(INIT_SQL.read_text())
        finally:
            await conn.close()

    asyncio.run(_go())
    _schema_initialized = True


@pytest.fixture(scope="session")
def database_dsn() -> str:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL not set — skipping service integration tests")
    _ensure_schema_once(dsn)
    return _async_dsn(dsn)


@pytest_asyncio.fixture
async def async_engine(database_dsn: str):
    """Fresh engine per test — binds to the current test's event loop."""
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(database_dsn, poolclass=NullPool)

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE "
                "control_plane.session_events, "
                "control_plane.approvals, "
                "control_plane.sessions, "
                "control_plane.schedules, "
                "control_plane.agents, "
                "task_queue.task_events, "
                "task_queue.tasks "
                "RESTART IDENTITY CASCADE"
            )
        )

    import shared.lib.db as _db

    _db.engine = engine
    _db.async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    _db._schema_ready = False

    # Platform modules bind `async_session_factory` at import time via
    # `from shared.lib.db import async_session_factory`. Rebind the name
    # inside every importer so they see this per-test engine.
    import sys

    for modname in (
        "gateway.provisioner",
        "gateway.scheduler",
        "gateway.message",
        "gateway.api",
        "gateway.event_collector",
    ):
        mod = sys.modules.get(modname)
        if mod is not None and hasattr(mod, "async_session_factory"):
            mod.async_session_factory = _db.async_session_factory

    from shared.lib.db import ensure_runtime_schema

    await ensure_runtime_schema()

    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(async_engine):
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
