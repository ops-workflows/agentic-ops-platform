"""Real-container smoke coverage for object-store-backed Knowledge MCP serving."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import docker
import pytest
from docker.errors import ImageNotFound
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from sqlalchemy import delete
from sqlalchemy.engine import make_url

from shared.lib.knowledge_source_graphify import (
    GRAPHIFY_VERSION,
    build_knowledge_source_bundle,
    validate_graphify_output,
)
from shared.lib.knowledge_source_publication import publish_knowledge_source_bundle
from shared.lib.models import KnowledgeSource, KnowledgeSourceVersion
from shared.lib.object_store import S3ObjectStore

pytestmark = pytest.mark.scenario

MINIO_ACCESS_KEY = "knowledge_test"
MINIO_SECRET_KEY = "knowledge-test-secret"
KNOWLEDGE_BUCKET = "knowledge-smoke"


def _wait_for_port(container, container_port: str, timeout_sec: float = 30) -> int:
    deadline = time.monotonic() + timeout_sec
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        container.reload()
        if container.status == "exited":
            pytest.fail(container.logs(tail=200).decode("utf-8", errors="replace"))
        bindings = container.attrs["NetworkSettings"]["Ports"].get(container_port)
        if bindings:
            host_port = int(bindings[0]["HostPort"])
            try:
                with socket.create_connection(("127.0.0.1", host_port), timeout=0.25):
                    return host_port
            except OSError as exc:
                last_error = exc
        time.sleep(0.1)
    pytest.fail(f"Container port {container_port} was not ready: {last_error}")


def _wait_for_minio(container, timeout_sec: float = 30) -> int:
    host_port = _wait_for_port(container, "9000/tcp", timeout_sec=timeout_sec)
    deadline = time.monotonic() + timeout_sec
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        container.reload()
        if container.status == "exited":
            pytest.fail(container.logs(tail=200).decode("utf-8", errors="replace"))
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{host_port}/minio/health/ready",
                timeout=0.5,
            ) as response:
                if response.status == 200:
                    return host_port
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(0.1)
    logs = container.logs(tail=200).decode("utf-8", errors="replace")
    pytest.fail(f"MinIO was not ready: {last_error}\nLogs:\n{logs}")


async def _wait_for_mcp(container, host_port: int, timeout_sec: float = 30) -> None:
    deadline = time.monotonic() + timeout_sec
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        container.reload()
        if container.status == "exited":
            pytest.fail(container.logs(tail=200).decode("utf-8", errors="replace"))
        transport = StreamableHttpTransport(
            f"http://127.0.0.1:{host_port}/mcp",
            headers={"x-task-workflow": "platform-test"},
        )
        try:
            async with Client(transport, timeout=3) as client:
                if await client.ping():
                    return
        except Exception as exc:
            last_error = exc
        await asyncio.sleep(0.1)
    logs = container.logs(tail=200).decode("utf-8", errors="replace")
    pytest.fail(f"Knowledge MCP was not ready: {last_error!r}\nLogs:\n{logs}")


def _build_bundle(tmp_path: Path, source: KnowledgeSource, version: KnowledgeSourceVersion):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "app.py").write_text(
        "def knowledge_smoke():\n    return 'object-store-backed'\n",
        encoding="utf-8",
    )
    graphify_dir = tmp_path / "graphify" / "graphify-out"
    graphify_dir.mkdir(parents=True)
    (graphify_dir / "graph.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "function:knowledge_smoke",
                        "name": "knowledge_smoke",
                        "source_file": "app.py",
                        "line": 1,
                    }
                ],
                "links": [],
                "hyperedges": [],
            }
        ),
        encoding="utf-8",
    )
    (graphify_dir / "manifest.json").write_text(
        json.dumps({"app.py": {"mtime": 1.0, "ast_hash": "abc", "semantic_hash": ""}}),
        encoding="utf-8",
    )
    (graphify_dir / "GRAPH_REPORT.md").write_text("# Knowledge smoke\n", encoding="utf-8")
    extraction = validate_graphify_output(graphify_dir)
    return build_knowledge_source_bundle(
        source_id=str(source.id),
        canonical_alias=source.canonical_alias,
        repository_url=source.repository_url,
        commit_sha=version.commit_sha,
        source_dir=source_dir,
        graphify=extraction,
        bundle_dir=tmp_path / "bundle",
        include_paths=source.include_paths,
        exclude_paths=source.exclude_paths,
        extraction_config_hash=version.extraction_config_hash,
    )


@pytest.mark.asyncio
async def test_knowledge_mcp_hydrates_minio_bundle_and_serves_source(
    require_runtime,
    async_engine,
    db_session,
    tmp_path: Path,
    repo_root: Path,
) -> None:
    docker_client = docker.from_env()
    network = os.environ.get("DOCKER_NETWORK", "aiops-test-network")
    image = os.environ.get("MCP_TEST_IMAGE", "ai-ops-mcp:latest")
    try:
        docker_client.images.get(image)
    except ImageNotFound:
        pytest.fail(f"Knowledge MCP smoke image is missing: {image}; run make mcp-build")

    suffix = uuid.uuid4().hex[:12]
    minio_name = f"knowledge-minio-{suffix}"
    mcp_name = f"knowledge-mcp-{suffix}"
    minio = docker_client.containers.run(
        "minio/minio",
        command=["server", "/data"],
        detach=True,
        name=minio_name,
        network=network,
        ports={"9000/tcp": None},
        environment={
            "MINIO_ROOT_USER": MINIO_ACCESS_KEY,
            "MINIO_ROOT_PASSWORD": MINIO_SECRET_KEY,
        },
    )
    mcp = None
    source = KnowledgeSource(
        id=uuid.uuid4(),
        canonical_alias=f"knowledge-smoke-{suffix}",
        repository_url="https://example.com/acme/knowledge-smoke.git",
        default_ref="main",
        include_paths=["**"],
        exclude_paths=[],
        sync_policy={},
        enabled=True,
    )
    version = KnowledgeSourceVersion(
        id=uuid.uuid4(),
        source_id=source.id,
        commit_sha="a" * 40,
        status="succeeded",
        graphify_version=GRAPHIFY_VERSION,
        extraction_config_hash="b" * 64,
        file_count=1,
        node_count=1,
        edge_count=0,
        warnings=[],
    )
    try:
        minio_port = _wait_for_minio(minio)
        store = S3ObjectStore(
            endpoint=f"127.0.0.1:{minio_port}",
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
        )
        store.ensure_bucket(KNOWLEDGE_BUCKET)
        publication = publish_knowledge_source_bundle(
            _build_bundle(tmp_path, source, version),
            bucket=KNOWLEDGE_BUCKET,
            object_store=store,
        )
        version.artifact_keys = publication.artifact_keys
        version.artifact_checksums = publication.artifact_checksums
        db_session.add(source)
        db_session.add(version)
        await db_session.flush()
        source.current_successful_version_id = version.id
        await db_session.commit()

        database_url = make_url(os.environ["TEST_DATABASE_URL"])
        fixture_root = repo_root / "tests" / "fixtures" / "knowledge-mcp-container"
        mcp = docker_client.containers.run(
            image,
            command=[
                "python",
                "-m",
                "uvicorn",
                "mcps.core.mcp_knowledge:app",
                "--host",
                "0.0.0.0",
                "--port",
                "8108",
            ],
            detach=True,
            name=mcp_name,
            network=network,
            ports={"8108/tcp": None},
            environment={
                "PLATFORM_CONFIG_FILE": "/app/platform-config.yaml",
                "PG_HOST": "postgres",
                "PG_PORT": "5432",
                "PG_DB": database_url.database or "agentic_ops_test",
                "PG_USER": database_url.username or "agentic_ops",
                "PG_PASSWORD": database_url.password or "localdev-postgres-password",
                "OBJECT_STORE_PROVIDER": "s3",
                "OBJECT_STORE_ENDPOINT": f"{minio_name}:9000",
                "OBJECT_STORE_ACCESS_KEY": MINIO_ACCESS_KEY,
                "OBJECT_STORE_SECRET_KEY": MINIO_SECRET_KEY,
                "KNOWLEDGE_SOURCE_OBJECT_STORE_BUCKET": KNOWLEDGE_BUCKET,
                "KNOWLEDGE_SOURCE_SERVING_CACHE_ROOT": "/tmp/knowledge-cache",
                "KNOWLEDGE_SOURCE_REFRESH_INTERVAL_SEC": "3600",
                "KNOWLEDGE_SOURCE_CACHE_VERSIONS_TO_KEEP": "2",
                "WORKFLOW_ROOT": "/app/workflows",
                "WORKFLOW_REPO_PATHS": "/app/workflows",
            },
            volumes={
                str(fixture_root / "platform-config.yaml"): {
                    "bind": "/app/platform-config.yaml",
                    "mode": "ro",
                },
                str(fixture_root / "workflows"): {
                    "bind": "/app/workflows",
                    "mode": "ro",
                },
            },
        )
        mcp_port = _wait_for_port(mcp, "8108/tcp")
        await _wait_for_mcp(mcp, mcp_port)
        transport = StreamableHttpTransport(
            f"http://127.0.0.1:{mcp_port}/mcp",
            headers={"x-task-workflow": "platform-test"},
        )
        async with Client(transport, timeout=30) as client:
            listed = await client.call_tool("list_sources", {})
            searched = await client.call_tool(
                "search_source",
                {"source_alias": source.canonical_alias, "query": "object-store-backed"},
            )

        assert listed.data == [
            {
                "alias": source.canonical_alias,
                "commit_sha": version.commit_sha,
                "version_id": str(version.id),
                "status": "ready",
            }
        ]
        assert searched.data["matches"][0]["path"] == "app.py"
        assert searched.data["matches"][0]["preview"] == "return 'object-store-backed'"
        assert mcp.exec_run(["test", "-f", f"/tmp/knowledge-cache/{source.id}/{version.id}/.ready"]).exit_code == 0
    finally:
        if mcp is not None:
            mcp.remove(force=True)
        minio.remove(force=True)
        await db_session.execute(delete(KnowledgeSource).where(KnowledgeSource.id == source.id))
        await db_session.commit()
