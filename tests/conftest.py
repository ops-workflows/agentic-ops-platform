"""Root pytest configuration and shared fixtures."""

from __future__ import annotations

import getpass
import importlib
import os
import socket
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest
import uvicorn

TESTS_ROOT = Path(__file__).resolve().parent
REPO_ROOT = TESTS_ROOT.parent
FIXTURE_REPO_ROOT = TESTS_ROOT / "fixtures" / "repo-root"


# ── Alias hyphenated top-level packages so they can be imported ──
# The repo layout uses `session-manager/` (hyphen), but the code inside
# imports itself as `session_manager` (underscore) — which works only
# when the Dockerfile copies the directory under the underscore name.
# For tests we load it by file path and register the alias.
def _register_hyphenated_package_aliases() -> None:
    hyphenated_to_underscore = {
        "session-manager": "session_manager",
    }
    for src, alias in hyphenated_to_underscore.items():
        if alias in sys.modules:
            continue
        init_path = REPO_ROOT / src / "__init__.py"
        if not init_path.exists():
            continue
        spec = importlib.util.spec_from_file_location(
            alias, init_path, submodule_search_locations=[str(REPO_ROOT / src)]
        )
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[alias] = module
        spec.loader.exec_module(module)


_register_hyphenated_package_aliases()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "unit: Layer 0 pure unit tests")
    config.addinivalue_line("markers", "service: Layer 1 service integration tests (real Postgres)")
    config.addinivalue_line("markers", "scenario: Layer 2 runtime scenario tests (real runtime container)")
    config.addinivalue_line("markers", "compat: Layer 3 compatibility tests")


# ── Env helpers ─────────────────────────────────────────────────


def _have_database() -> bool:
    return bool(os.environ.get("TEST_DATABASE_URL"))


def _have_minio() -> bool:
    return bool(os.environ.get("TEST_MINIO_ENDPOINT"))


def _runtime_enabled() -> bool:
    return os.environ.get("TEST_RUNTIME_ENABLED") == "1"


@pytest.fixture(scope="session")
def tests_root() -> Path:
    return TESTS_ROOT


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def fixture_repo_root() -> Path:
    return FIXTURE_REPO_ROOT


@pytest.fixture(scope="session")
def fixture_workflows_dir(fixture_repo_root: Path) -> Path:
    return fixture_repo_root / "workflows"


@pytest.fixture(scope="session")
def test_workflow_dir(fixture_workflows_dir: Path) -> Path:
    return fixture_workflows_dir / "platform-test"


@pytest.fixture(scope="session")
def test_plugin_dir(test_workflow_dir: Path) -> Path:
    return test_workflow_dir


# ── Skip helpers ─────────────────────────────────────────────────


@pytest.fixture
def require_database() -> str:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL not set — skipping service integration test")
    return dsn


@pytest.fixture
def require_minio() -> tuple[str, str, str]:
    endpoint = os.environ.get("TEST_MINIO_ENDPOINT")
    if not endpoint:
        pytest.skip("TEST_MINIO_ENDPOINT not set — skipping MinIO-dependent test")
    return (
        endpoint,
        os.environ.get("TEST_MINIO_ACCESS_KEY", "agentic_ops"),
        os.environ.get("TEST_MINIO_SECRET_KEY", "agentic-ops-test-secret"),
    )


@pytest.fixture
def require_runtime() -> None:
    if not _runtime_enabled():
        pytest.skip("TEST_RUNTIME_ENABLED != 1 — skipping runtime scenario test")

    if not os.environ.get("DOCKER_HOST"):
        rancher_socket = Path(f"/Users/{getpass.getuser()}/.rd/docker.sock")
        if rancher_socket.exists():
            os.environ["DOCKER_HOST"] = f"unix://{rancher_socket}"


# ── Free port helper for fake services ─────────────────────────


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def free_port() -> int:
    return _free_port()


class _UvicornServer:
    """Run a FastAPI app in a background thread for the duration of a test."""

    def __init__(self, app, port: int) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"

    def start(self) -> None:
        self._thread.start()
        deadline = time.time() + 10
        while time.time() < deadline and not self._server.started:
            time.sleep(0.05)
        if not self._server.started:
            raise RuntimeError("Fake service failed to start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


def run_app_in_background(app, port: int | None = None) -> _UvicornServer:
    server = _UvicornServer(app, port or _free_port())
    server.start()
    return server


# Expose the helper as a fixture factory
@pytest.fixture
def background_app():
    servers: list[_UvicornServer] = []

    def _factory(app, port: int | None = None) -> _UvicornServer:
        server = run_app_in_background(app, port)
        servers.append(server)
        return server

    yield _factory

    for server in servers:
        server.stop()
