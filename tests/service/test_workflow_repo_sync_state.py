"""Service-layer tests for the workflow-repo sync pipeline's Postgres state.

Exercises pin/sync/read against a real Postgres control_plane.workflow_repo_state
row. The git clone/fetch and bundle build steps are monkeypatched — these
tests are about state persistence, not the sync pipeline's git/bundle
mechanics (covered by unit tests elsewhere).
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete

from shared.lib.config import settings
from shared.lib.models import WorkflowRepoState

pytestmark = pytest.mark.service


@pytest.fixture(autouse=True)
async def _reset_workflow_repo_state(async_engine, db_session):  # noqa: ARG001
    await db_session.execute(delete(WorkflowRepoState))
    await db_session.commit()
    yield
    await db_session.execute(delete(WorkflowRepoState))
    await db_session.commit()


@pytest.mark.asyncio
async def test_pin_workflow_repo_ref_persists_and_reads_back(db_session) -> None:
    from shared.lib.workflow_repo_sync import get_workflow_repo_state, pin_workflow_repo_ref

    await pin_workflow_repo_ref(db_session, "v1.2.3")

    state = await get_workflow_repo_state(db_session)
    assert state is not None
    assert state.pinned_ref == "v1.2.3"


@pytest.mark.asyncio
async def test_sync_workflow_repo_records_success(db_session, monkeypatch, tmp_path) -> None:
    from shared.lib import workflow_repo_sync as sync_mod

    monkeypatch.setattr(settings, "workflow_repo_url", "")
    monkeypatch.setattr(settings, "runtime_bundle_root", "")

    class _FakePackage:
        def __init__(self, name: str) -> None:
            self.name = name

    monkeypatch.setattr(sync_mod, "discover_workflow_packages", lambda: [_FakePackage("platform-test")])
    monkeypatch.setattr(sync_mod, "git_commit_for_path", lambda _path: "abc1234")

    result = await sync_mod.sync_workflow_repo(db_session)

    assert result.status == "ok"
    assert result.discovered_workflows == ["platform-test"]
    assert result.bundle_errors == {}

    state = await sync_mod.get_workflow_repo_state(db_session)
    assert state is not None
    assert state.last_sync_status == "ok"
    assert state.discovered_workflows == ["platform-test"]
    assert state.last_synced_at is not None


@pytest.mark.asyncio
async def test_sync_workflow_repo_records_error_without_raising(db_session, monkeypatch) -> None:
    from shared.lib import workflow_repo_sync as sync_mod

    monkeypatch.setattr(settings, "workflow_repo_url", "https://github.com/acme/workflows.git")

    def _raise_sync(_ref):
        raise RuntimeError("simulated git failure")

    monkeypatch.setattr(sync_mod, "sync_workflow_repo_to_ref", _raise_sync)

    result = await sync_mod.sync_workflow_repo(db_session)

    assert result.status == "error"
    assert "simulated git failure" in (result.error or "")

    state = await sync_mod.get_workflow_repo_state(db_session)
    assert state is not None
    assert state.last_sync_status == "error"
    assert state.last_sync_error and "simulated git failure" in state.last_sync_error
