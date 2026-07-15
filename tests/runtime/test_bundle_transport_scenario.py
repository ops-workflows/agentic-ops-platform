"""Layer 2 — object-storage-backed workflow bundle transport.

Covers the new https bundle URI path: session-manager hands the runtime an
https bundle URI (as it would after presigning an object-store upload), and
the runtime entrypoint downloads + safely extracts the tarball instead of
reading a locally mounted plugin directory.

Requires Docker + ``ai-ops-agent-runtime:latest`` + TEST_RUNTIME_ENABLED=1.
"""

from __future__ import annotations

import io
import tarfile

import pytest
from fastapi import FastAPI
from fastapi.responses import Response

from tests.conftest import REPO_ROOT, run_app_in_background
from tests.fakes.mock_llm import Turn

pytestmark = pytest.mark.scenario


@pytest.mark.asyncio
async def test_runtime_stages_workspace_from_https_bundle_uri(
    require_runtime,
    mock_llm,
    fake_mattermost,
    create_task,
    spawn_and_wait,
    merged_repo_root,
    tmp_path,
) -> None:
    from shared.lib.config import settings
    from shared.lib.workflow_bundles import build_workflow_bundle

    build_workflow_bundle(
        workflow="platform-test",
        output_dir=tmp_path / "bundles",
        platform_root=REPO_ROOT,
        workflow_roots=[merged_repo_root],
    )
    bundle_dir = tmp_path / "bundles" / "platform-test"

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(bundle_dir, arcname=".")
    tarball_bytes = buffer.getvalue()

    app = FastAPI()

    @app.get("/bundle.tar.gz")
    def _serve_bundle() -> Response:
        return Response(content=tarball_bytes, media_type="application/gzip")

    server = run_app_in_background(app, host="0.0.0.0")

    original_uri_template = settings.runtime_bundle_uri_template
    original_bundle_root = settings.runtime_bundle_root
    settings.runtime_bundle_uri_template = f"http://host.docker.internal:{server.port}/bundle.tar.gz"
    settings.runtime_bundle_root = ""

    try:
        mock_llm.set_scenario(
            [Turn(respond=[{"type": "text", "text": "Bundle staged via https."}], stop_reason="end_turn")]
        )

        task = await create_task(prompt="Confirm the workspace staged correctly from an https bundle.")
        exit_code, logs = await spawn_and_wait(task, timeout_sec=120)

        assert exit_code == 0, f"Container exited {exit_code}.\nLogs:\n{logs}"
    finally:
        settings.runtime_bundle_uri_template = original_uri_template
        settings.runtime_bundle_root = original_bundle_root
        server.stop()
