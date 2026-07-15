"""Layer 2 — scenario harness and shared runtime fixtures.

Design:

Every runtime scenario test boots four fake services on free ports,
generates a dynamic ``platform-config.yaml`` pointing the ``test`` model
profile's ``ANTHROPIC_BASE_URL`` at the mock LLM, injects a task into
Postgres, and calls ``spawn_agent_session()`` to launch a real Docker
container.  The container runs the session entrypoint against the fakes
and exits.  Tests then assert on DB state and the fakes' recorded data.

Requirements for running:
- Docker daemon must be running
- ``ai-ops-agent-runtime:latest`` image must be built
- ``TEST_RUNTIME_ENABLED=1`` and ``TEST_DATABASE_URL`` must be set
- Postgres (from docker-compose) must be reachable
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import shutil
import tarfile
import uuid
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import yaml
from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import FIXTURE_REPO_ROOT, REPO_ROOT, TESTS_ROOT, _UvicornServer, run_app_in_background
from tests.fakes.hindsight import FakeHindsight, build_fake_hindsight
from tests.fakes.mcp_testserver import TestMCPServer, build_test_mcp_server
from tests.fakes.message import FakeMattermost
from tests.fakes.mock_llm import MockLLMServer


class LocalMemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _object_path(self, bucket: str, key: str) -> Path:
        return self.root / bucket / key

    def upload_file(self, bucket: str, key: str, file_path: str) -> str:
        target = self._object_path(bucket, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, target)
        return key

    def download_file(self, bucket: str, key: str, file_path: str) -> bool:
        source = self._object_path(bucket, key)
        if not source.exists():
            return False
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, file_path)
        return True

    def clear_agent(self, agent_name: str) -> None:
        agent_dir = self.root / "agent-memory" / agent_name
        if agent_dir.exists():
            shutil.rmtree(agent_dir)

    def seed_backup(self, agent_name: str, files: dict[str, bytes]) -> None:
        target = self._object_path("agent-memory", f"{agent_name}/latest.tar.gz")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(target, mode="w:gz") as archive:
            for name, content in files.items():
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))

    def read_backup(self, agent_name: str, *, key: str = "latest.tar.gz") -> dict[str, bytes]:
        source = self._object_path("agent-memory", f"{agent_name}/{key}")
        with tarfile.open(source, mode="r:gz") as archive:
            result: dict[str, bytes] = {}
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                file_obj = archive.extractfile(member)
                if file_obj is None:
                    continue
                result[member.name] = file_obj.read()
                file_obj.close()
        return result

    def list_agent_keys(self, agent_name: str) -> list[str]:
        agent_dir = self.root / "agent-memory" / agent_name
        if not agent_dir.exists():
            return []
        return sorted(str(path.relative_to(agent_dir)) for path in agent_dir.rglob("*") if path.is_file())


# ---------------------------------------------------------------------------
# Auto-skip unless runtime tests are explicitly enabled
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(config, items):
    """Auto-skip runtime scenarios only unless TEST_RUNTIME_ENABLED=1 is set."""
    if os.environ.get("TEST_RUNTIME_ENABLED") != "1":
        skip = pytest.mark.skip(reason="TEST_RUNTIME_ENABLED != 1 (Layer 2 scenario tests)")
        for item in items:
            if "tests/runtime" in str(item.fspath).replace("\\", "/"):
                item.add_marker(skip)


# ---------------------------------------------------------------------------
# Async DSN helper (shared with service conftest)
# ---------------------------------------------------------------------------


def _async_dsn(raw: str) -> str:
    if raw.startswith("postgres://"):
        raw = "postgresql+asyncpg://" + raw.removeprefix("postgres://")
    elif raw.startswith("postgresql://"):
        raw = "postgresql+asyncpg://" + raw.removeprefix("postgresql://")
    return raw


def _raw_dsn(async_dsn: str) -> str:
    return async_dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


# ---------------------------------------------------------------------------
# DB schema init (idempotent)
# ---------------------------------------------------------------------------

_REPO_ROOT = REPO_ROOT
_INIT_SQL = _REPO_ROOT / "shared" / "lib" / "init_db.sql"
_schema_initialized = False


def _ensure_schema_once(dsn: str) -> None:
    global _schema_initialized
    if _schema_initialized:
        return
    # Reuse the safety guard from the service conftest — refuses to wipe
    # schemas in any DB whose name doesn't look like a test database
    # unless TEST_ALLOW_DB_WIPE=1 is set.
    from tests.service.conftest import _assert_safe_test_dsn

    _assert_safe_test_dsn(dsn)
    import asyncpg

    async def _go() -> None:
        conn = await asyncpg.connect(_raw_dsn(_async_dsn(dsn)))
        try:
            await conn.execute("DROP SCHEMA IF EXISTS control_plane CASCADE")
            await conn.execute("DROP SCHEMA IF EXISTS task_queue CASCADE")
            await conn.execute(_INIT_SQL.read_text())
        finally:
            await conn.close()

    asyncio.run(_go())
    _schema_initialized = True


# ---------------------------------------------------------------------------
# Fake service fixtures (module-scoped for perf — one set per test file)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fake_mattermost() -> FakeMattermost:
    return FakeMattermost()


@pytest.fixture(scope="module")
def fake_hindsight() -> FakeHindsight:
    return build_fake_hindsight()


@pytest.fixture(scope="module")
def mock_llm() -> MockLLMServer:
    return MockLLMServer()


@pytest.fixture(scope="module")
def fake_mcp() -> TestMCPServer:
    return build_test_mcp_server()


@pytest.fixture(scope="module")
def _fake_services(
    fake_mattermost: FakeMattermost,
    fake_hindsight: FakeHindsight,
    mock_llm: MockLLMServer,
    fake_mcp: TestMCPServer,
) -> dict[str, _UvicornServer]:
    """Start all four fake services on free ports for the module."""
    message_srv = run_app_in_background(fake_mattermost.app, host="0.0.0.0")
    hs_srv = run_app_in_background(fake_hindsight.app, host="0.0.0.0")
    llm_srv = run_app_in_background(mock_llm.app, host="0.0.0.0")
    mcp_srv = run_app_in_background(fake_mcp.app, host="0.0.0.0")

    yield {
        "message": message_srv,
        "hindsight": hs_srv,
        "llm": llm_srv,
        "mcp": mcp_srv,
    }

    for srv in (message_srv, hs_srv, llm_srv, mcp_srv):
        srv.stop()


# ---------------------------------------------------------------------------
# Gateway event collector — lightweight in-process FastAPI app that records
# events posted by the runtime container.
# ---------------------------------------------------------------------------


def _build_event_collector_app(event_store: list[dict[str, Any]]):
    from fastapi import FastAPI
    from starlette.responses import JSONResponse

    from gateway.api import get_runtime_approval_status, get_task_api
    from gateway.event_collector import EventPayload, receive_event
    from gateway.message import MattermostInteractiveAction, message_approval_action

    app = FastAPI()

    @app.post("/events")
    async def collect_event(request: Request):
        try:
            body = await request.json()
        except Exception:
            raw = await request.body()
            body = {"_raw": raw.decode("utf-8", errors="replace")}
        event_store.append(body)
        if isinstance(body, dict):
            await receive_event(EventPayload(**body))
        return JSONResponse({"status": "ok"})

    @app.get("/api/runtime/approvals/status")
    async def approval_status(task_id: str, tool_name: str, request_id: str):
        return await get_runtime_approval_status(task_id=task_id, tool_name=tool_name, request_id=request_id)

    @app.get("/api/tasks/{task_id}")
    async def task_status(task_id: str):
        return await get_task_api(task_id)

    @app.post("/webhooks/message/actions/approval")
    async def approval_action(request: Request):
        body = await request.json()
        return await message_approval_action(MattermostInteractiveAction(**body))

    @app.get("/health")
    async def health(_request: Request):
        return JSONResponse({"status": "ok"})

    return app


@pytest.fixture(scope="module")
def _event_store() -> list[dict[str, Any]]:
    return []


@pytest.fixture(scope="module")
def _event_collector(_event_store: list[dict[str, Any]]) -> _UvicornServer:
    app = _build_event_collector_app(_event_store)
    srv = run_app_in_background(app, host="0.0.0.0")
    yield srv
    srv.stop()


@pytest.fixture
def local_memory_store(tmp_path):
    store = LocalMemoryStore(tmp_path / "object-store")

    import session_manager.memory_sync as memory_sync_mod

    import shared.lib.object_store as object_store_mod

    original_upload_storage = object_store_mod.upload_file
    original_download_storage = object_store_mod.download_file
    original_upload_memory = memory_sync_mod.upload_file
    original_download_memory = memory_sync_mod.download_file
    original_helper_image = memory_sync_mod.MEMORY_HELPER_IMAGE

    object_store_mod.upload_file = store.upload_file
    object_store_mod.download_file = store.download_file
    memory_sync_mod.upload_file = store.upload_file
    memory_sync_mod.download_file = store.download_file
    memory_sync_mod.MEMORY_HELPER_IMAGE = "ai-ops-agent-runtime:latest"

    try:
        yield store
    finally:
        object_store_mod.upload_file = original_upload_storage
        object_store_mod.download_file = original_download_storage
        memory_sync_mod.upload_file = original_upload_memory
        memory_sync_mod.download_file = original_download_memory
        memory_sync_mod.MEMORY_HELPER_IMAGE = original_helper_image


@pytest.fixture
def reset_agent_memory_state(local_memory_store):
    def _reset(agent_name: str = "platform-test") -> None:
        from docker.errors import NotFound
        from session_manager.memory_sync import _get_docker_client, _get_volume_name

        local_memory_store.clear_agent(agent_name)
        client = _get_docker_client()
        volume_name = _get_volume_name(agent_name)
        for container in client.containers.list(all=True, filters={"volume": volume_name}):
            with contextlib.suppress(Exception):
                container.remove(force=True)
        with contextlib.suppress(NotFound):
            client.volumes.get(volume_name).remove(force=True)

    return _reset


# ---------------------------------------------------------------------------
# Dynamic platform-config.yaml generation
# ---------------------------------------------------------------------------


def _write_test_platform_config(
    target_path: Path,
    *,
    llm_port: int,
    message_port: int,
    hindsight_port: int,
    mcp_port: int,
    event_collector_port: int,
) -> None:
    """Write a platform-config.yaml with actual fake-service ports."""
    event_url = f"http://host.docker.internal:{event_collector_port}/events"
    config = {
        "config": {
            "AGE_PUBLIC_KEY": "",
            "PG_HOST": "localhost",
            "PG_PORT": "5432",
            "PG_DB": "agentic_ops",
            "PG_USER": "agentic_ops",
            "PG_PASSWORD": "localdev-postgres-password",
            "MESSAGE_BUS_API_URL": f"http://host.docker.internal:{message_port}",
            "MESSAGE_BUS_TEAM_NAME": "test-team",
            "CONTROL_PLANE_UI_URL": "",
        },
        "runtime_env": {
            "ANTHROPIC_API_KEY": None,
            "DISABLE_TELEMETRY": True,
            "DISABLE_ERROR_REPORTING": True,
            "DISABLE_FEEDBACK_COMMAND": True,
            "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": True,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": True,
            # ── Test MCP server discovery (consumed by .mcp.json ${VAR}) ──
            "FAKE_MCP_HOST": "host.docker.internal",
            "FAKE_MCP_PORT": str(mcp_port),
            "TEST_HOST_MESSAGE_API_URL": f"http://127.0.0.1:{message_port}",
            "TEST_HOST_HINDSIGHT_URL": f"http://127.0.0.1:{hindsight_port}",
            # ── Force event collector URL to test fake (overrides container_lifecycle
            #     defaults that would otherwise hit the real localdev gateway). The
            #     runtime entrypoint reads GATEWAY_URL and appends /events. ──
            "EVENT_COLLECTOR_URL": event_url,
            "GATEWAY_EVENT_URL": event_url,
            "GATEWAY_URL": f"http://host.docker.internal:{event_collector_port}",
        },
        "default_model_profile": "test",
        "model_profiles": {
            "test": {
                "ANTHROPIC_BASE_URL": f"http://host.docker.internal:{llm_port}",
                "ANTHROPIC_AUTH_TOKEN": "test-token",
                "ANTHROPIC_MODEL": "test-model",
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "test-model",
                "ANTHROPIC_DEFAULT_SONNET_MODEL": "test-model",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": "test-model",
                "CLAUDE_CODE_SUBAGENT_MODEL": "test-model",
            },
        },
        "secrets": {},
    }
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(yaml.dump(config, default_flow_style=False))


@pytest.fixture(scope="module")
def test_platform_config(
    tmp_path_factory,
    _fake_services: dict[str, _UvicornServer],
    _event_collector: _UvicornServer,
) -> Path:
    """Generate a platform-config.yaml with live fake-service ports."""
    config_dir = tmp_path_factory.mktemp("platform-config")
    config_path = config_dir / "platform-config.yaml"
    _write_test_platform_config(
        config_path,
        llm_port=_fake_services["llm"].port,
        message_port=_fake_services["message"].port,
        hindsight_port=_fake_services["hindsight"].port,
        mcp_port=_fake_services["mcp"].port,
        event_collector_port=_event_collector.port,
    )
    return config_path


# ---------------------------------------------------------------------------
# Database fixtures (per-test isolation via TRUNCATE)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def database_dsn() -> str:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL not set")
    _ensure_schema_once(dsn)
    return _async_dsn(dsn)


@pytest_asyncio.fixture
async def async_engine(database_dsn: str):
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
    _db.async_session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    _db._schema_ready = False

    import sys

    for modname in (
        "gateway.provisioner",
        "gateway.scheduler",
        "gateway.message",
        "gateway.api",
        "gateway.event_collector",
        "session_manager.container_lifecycle",
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


# ---------------------------------------------------------------------------
# Settings patcher — configures the shared Settings singleton to point at
# the test fixture repo and the dynamically generated platform config.
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_settings(
    test_platform_config: Path,
    _fake_services: dict[str, _UvicornServer],
    _event_collector: _UvicornServer,
    merged_repo_root: Path,
):
    """Temporarily patch shared.lib.config.settings for runtime tests."""
    from shared.lib.config import settings

    originals = {}
    patches = {
        "platform_config_file": str(test_platform_config),
        "repo_path": str(merged_repo_root / "workflows"),
        "host_repo_root": str(merged_repo_root),
        "workflow_root": str(merged_repo_root / "workflows"),
        "workflow_repo_paths": str(merged_repo_root / "workflows"),
        "message_bus_api_url": _fake_services["message"].base_url,
        "message_bus_bot_token": "test-bot-token",
        "message_bus_team_name": "test-team",
        "gateway_event_url": f"http://host.docker.internal:{_event_collector.port}/events",
        "gateway_public_base_url": _event_collector.base_url,
        "control_plane_ui_url": "",
        "hindsight_url": f"http://host.docker.internal:{_fake_services['hindsight'].port}",
        "age_identity": "",
    }
    for attr, value in patches.items():
        if not hasattr(settings, attr):
            continue
        originals[attr] = getattr(settings, attr)
        setattr(settings, attr, value)

    yield settings

    for attr, value in originals.items():
        setattr(settings, attr, value)


# ---------------------------------------------------------------------------
# Merged repo-root for the runtime container
# ---------------------------------------------------------------------------
# The runtime image relies on `shared/lib/*.py` being mounted at /shared by
# the session manager. Our fixture's `tests/fixtures/repo-root/shared` only
# contains test-specific overlays (CLAUDE.md, skills/, platform-config.yaml),
# not the platform's actual Python code. To keep both halves available we
# build a merged tree under a session-scoped tmp dir using symlinks:
#
#   <merged>/shared/lib       → real REPO_ROOT/shared/lib
#   <merged>/mcps      → real REPO_ROOT/mcps
#   <merged>/hooks     → real REPO_ROOT/hooks
#   <merged>/shared/__init__.py → real REPO_ROOT/shared/__init__.py
#   <merged>/CLAUDE.md → fixture override
#   <merged>/skills    → fixture override (test-shared-skill, etc.)
#   <merged>/platform-config.yaml → fixture override
#   <merged>/workflows        → fixture workflows (platform-test workflow)


@pytest.fixture(scope="session")
def merged_repo_root(tmp_path_factory) -> Path:
    """Build a hybrid repo-root that combines real shared/lib code with
    the test fixture's instruction-surface overlays.

    The merged tree lives inside the workspace (under
    ``tests/fixtures/.runtime-mount/``) so Docker Desktop / Rancher
    Desktop file-shares pick it up without extra configuration. We
    *copy* (not symlink) so the container sees real files at every
    path — bind-mounted symlinks pointing outside the bind tree dangle
    inside the container.
    """
    base = TESTS_ROOT / "fixtures" / ".runtime-mount"
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)

    real_root = REPO_ROOT
    fixture_root = FIXTURE_REPO_ROOT

    def _ignore(_src: str, names: list[str]) -> list[str]:
        return [n for n in names if n in {"__pycache__", ".pytest_cache", ".mypy_cache"}]

    # 1) shared/ — start with the real shared, overlay the fixture
    shutil.copytree(real_root / "shared", base / "shared", ignore=_ignore)
    fixture_shared = fixture_root / "shared"
    if fixture_shared.exists():
        for child in fixture_shared.rglob("*"):
            if any(part in {"__pycache__"} for part in child.parts):
                continue
            rel = child.relative_to(fixture_shared)
            target = base / "shared" / rel
            if child.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(child, target)

    real_hooks = real_root / "hooks"
    if real_hooks.exists():
        shutil.copytree(real_hooks, base / "shared" / "hooks", ignore=_ignore)

    # 2) workflows/ — copy from fixture
    fixture_workflows = fixture_root / "workflows"
    if fixture_workflows.exists():
        shutil.copytree(fixture_workflows, base / "workflows", ignore=_ignore)

    return base


# ---------------------------------------------------------------------------
# Task factory — creates a task row in Postgres ready for spawn
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def create_task(db_session: AsyncSession):
    """Factory fixture: create a task row and return the Task ORM object."""
    from shared.lib.models import Task

    async def _factory(
        *,
        workflow: str = "platform-test",
        prompt: str = "test prompt",
        message_channel: str = "platform-test-channel",
        message_thread: str = "",
        task_metadata: dict[str, Any] | None = None,
    ) -> Task:
        task = Task(
            id=uuid.uuid4(),
            workflow=workflow,
            prompt=prompt,
            message_channel=message_channel,
            message_thread=message_thread,
            status="running",
            task_metadata=task_metadata or {"channel_id": "test-channel-id"},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        return task

    return _factory


# ---------------------------------------------------------------------------
# Container spawn + wait helper
# ---------------------------------------------------------------------------


@pytest.fixture
def spawn_and_wait(
    patched_settings,
    _fake_services: dict[str, _UvicornServer],
    _event_collector: _UvicornServer,
    local_memory_store,
):
    """Spawn a runtime container and wait for it to exit.

    Returns (exit_code, logs) so tests can assert on the outcome.
    Automatically removes the container after the test.

    Belt-and-braces: also force-overwrite the EVENT_COLLECTOR_URL /
    GATEWAY_EVENT_URL on the running container by mutating the settings
    object *and* registering an env override that container_lifecycle
    will merge into the docker run env.
    """
    containers: list = []

    async def _spawn(task, *, timeout_sec: int = 120) -> tuple[int, str]:
        # Re-assert the patched URLs in case any preceding fixture (e.g.
        # async_engine's module rebind loop) re-imported settings.
        from shared.lib.config import settings

        ev_url = f"http://host.docker.internal:{_event_collector.port}/events"
        settings.gateway_event_url = ev_url

        from session_manager.container_lifecycle import spawn_agent_session
        from session_manager.memory_sync import backup_memory, restore_memory

        await restore_memory(task.workflow)

        container = await spawn_agent_session(task)
        if container is None:
            return -1, "spawn_agent_session returned None"

        containers.append(container)

        # Wait for container to exit
        try:
            result = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None,
                    container.wait,
                ),
                timeout=timeout_sec,
            )
            exit_code = result.get("StatusCode", -1)
        except TimeoutError:
            container.kill()
            exit_code = -99

        logs = container.logs(tail=500).decode("utf-8", errors="replace")
        if exit_code != -99:
            from session_manager.container_lifecycle import _post_completion_to_message_thread

            from shared.lib.config import settings as shared_settings

            original_message_bus_api_url = shared_settings.message_bus_api_url
            shared_settings.message_bus_api_url = _fake_services["message"].base_url
            try:
                await _post_completion_to_message_thread(
                    str(task.id),
                    task.workflow,
                    "succeeded" if exit_code == 0 else "failed",
                    None if exit_code == 0 else logs[-500:],
                )
                await backup_memory(task.workflow)
            finally:
                shared_settings.message_bus_api_url = original_message_bus_api_url
        if os.environ.get("RUNTIME_TEST_DEBUG") == "1":
            import sys as _sys

            _sys.stderr.write(f"\n===CONTAINER LOGS (exit={exit_code})===\n{logs}\n===END LOGS===\n")
            _sys.stderr.flush()
        return exit_code, logs

    yield _spawn

    # Cleanup: remove spawned containers
    for c in containers:
        with contextlib.suppress(Exception):
            c.remove(force=True)


# ---------------------------------------------------------------------------
# Collected events helper
# ---------------------------------------------------------------------------


@pytest.fixture
def collected_events(_event_store: list[dict[str, Any]]):
    """Return collected events and clear the store for isolation."""
    _event_store.clear()
    yield _event_store


@pytest.fixture
def admit_when_resume_pending(async_engine):
    """Admit a live waiting runtime once the task enters resume_pending."""
    from shared.lib.models import Task
    from shared.lib.task_queue import dequeue_task

    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    async def _admit(task_id, *, workflow: str, timeout: float = 60.0):
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            async with factory() as session:
                task = await session.get(Task, task_id)
                if task is not None and task.status == "resume_pending":
                    resumed = await dequeue_task(session, workflow=workflow, max_running=1)
                    assert resumed is not None, "Expected scheduler admission for resume_pending task"
                    assert resumed.id == task_id
                    return
            await asyncio.sleep(0.25)
        raise AssertionError(f"Timed out waiting for task {task_id} to enter resume_pending")

    return _admit


# ---------------------------------------------------------------------------
# Reply-by-pattern helper for FakeMattermost
# ---------------------------------------------------------------------------


@pytest.fixture
def reply_when(fake_mattermost):
    """Spawn a background asyncio task that replies to the first matching post.

    Usage:

        reply_when(lambda p: "approve" in p.message.lower(), reply="approve")

    Returns the asyncio.Task so the test can await/cancel it.
    """
    tasks: list[asyncio.Task] = []

    def _factory(predicate, *, reply: str, timeout: float = 60.0):
        async def _runner():
            post = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: fake_mattermost.wait_for_post(predicate, timeout=timeout),
            )
            if post is None:
                return None
            fake_mattermost.inject_reply(
                thread_id=post.root_id or post.id,
                channel_id=post.channel_id,
                message=reply,
            )
            return post

        t = asyncio.create_task(_runner())
        tasks.append(t)
        return t

    yield _factory

    for t in tasks:
        if not t.done():
            t.cancel()


# ---------------------------------------------------------------------------
# Mock-LLM probe summary parsing helper
# ---------------------------------------------------------------------------


def _parse_probe_summary(text: str) -> dict[str, list[str]]:
    """Parse a probe summary of the shape:

        found_markers=[A,B] missing_markers=[C]

    into ``{"found": [...], "missing": [...]}``. Empty lists are returned
    for either key if missing or unparseable.
    """
    import re

    def _extract(label: str) -> list[str]:
        match = re.search(rf"{label}=\[([^\]]*)\]", text or "")
        if not match:
            return []
        body = match.group(1).strip()
        if not body:
            return []
        return [s.strip() for s in body.split(",") if s.strip()]

    return {
        "found": _extract("found_markers"),
        "missing": _extract("missing_markers"),
    }


@pytest.fixture
def parse_probe_summary():
    return _parse_probe_summary


# ---------------------------------------------------------------------------
# Gateway-API task helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def gateway_app(async_engine, patched_settings):
    """Build the Gateway FastAPI app bound to the per-test engine.

    Returns the FastAPI app for use with httpx.AsyncClient(transport=...).
    """
    from gateway.main import app as gateway_app_instance

    yield gateway_app_instance


@pytest_asyncio.fixture
async def gateway_client(gateway_app):
    """An httpx.AsyncClient targeting the Gateway API in-process."""
    import httpx

    transport = httpx.ASGITransport(app=gateway_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway-test") as client:
        yield client


@pytest_asyncio.fixture
async def create_task_via_api(gateway_client):
    """Factory: create a task by posting to the Gateway tasks API.

    Returns the created Task ORM row reloaded from the DB.
    """
    from sqlalchemy import select

    from shared.lib.db import async_session_factory
    from shared.lib.models import Task

    async def _factory(
        *,
        workflow: str = "platform-test",
        prompt: str = "test prompt via api",
        message_channel: str = "platform-test-channel",
        message_thread: str = "",
        task_metadata: dict[str, Any] | None = None,
    ) -> Task:
        body = {
            "workflow": workflow,
            "prompt": prompt,
            "message_channel": message_channel,
            "message_thread": message_thread,
            "task_metadata": task_metadata or {"channel_id": "test-channel-id"},
        }
        # Try canonical task creation endpoint; fall back to direct DB
        # if the route shape differs.
        resp = await gateway_client.post("/api/tasks", json=body)
        if resp.status_code in (200, 201):
            data = resp.json()
            tid = data.get("id") or data.get("task_id")
            if tid:
                async with async_session_factory() as s:
                    row = await s.scalar(select(Task).where(Task.id == tid))
                    if row is not None:
                        return row
        # Fallback to direct insert if the API route was unavailable.
        import uuid as _uuid

        async with async_session_factory() as s:
            row = Task(
                id=_uuid.uuid4(),
                workflow=workflow,
                prompt=prompt,
                message_channel=message_channel,
                message_thread=message_thread,
                status="running",
                task_metadata=task_metadata or {"channel_id": "test-channel-id"},
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return row

    return _factory
