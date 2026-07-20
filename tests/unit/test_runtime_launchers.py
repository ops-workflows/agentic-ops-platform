from __future__ import annotations

from unittest.mock import MagicMock

import docker.errors
import pytest
from session_manager.runtime_launchers import (
    DockerRuntimeLauncher,
    RuntimeLaunchSpec,
    get_runtime_launcher,
    reset_runtime_launcher_for_tests,
)

pytestmark = pytest.mark.unit


def test_docker_launcher_translates_spec_to_container_run(monkeypatch):
    monkeypatch.setattr("session_manager.runtime_launchers.sys.platform", "linux")
    monkeypatch.delenv("SANDBOX_MODE", raising=False)
    monkeypatch.setenv("DOCKER_NETWORK", "test-network")
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("missing")
    container = MagicMock()
    container.id = "container-id"
    container.short_id = "abc123"
    client.containers.run.return_value = container

    launcher = DockerRuntimeLauncher(client)
    handle = launcher.launch(
        RuntimeLaunchSpec(
            task_id="task-12345678",
            workflow="platform-test",
            image="runtime:latest",
            environment={"A": "1"},
            plugin_dir="/workflows/platform-test",
            shared_dir="/shared",
            memory_volume_name="agent-memory-platform-test",
            container_name="session-test",
        )
    )

    assert handle.id == "container-id"
    client.containers.run.assert_called_once()
    kwargs = client.containers.run.call_args.kwargs
    assert kwargs["image"] == "runtime:latest"
    assert kwargs["environment"] == {"A": "1"}
    assert kwargs["volumes"]["/workflows/platform-test"] == {"bind": "/plugin-src", "mode": "ro"}
    assert kwargs["volumes"]["agent-memory-platform-test"] == {"bind": "/memory", "mode": "rw"}
    assert kwargs["network"] == "test-network"
    assert kwargs["extra_hosts"] == {"host.docker.internal": "host-gateway"}
    assert kwargs["labels"]["agentic_ops.runtime_provider"] == "docker"
    assert "security_opt" not in kwargs
    assert "cap_add" not in kwargs


def test_docker_launcher_uses_macos_sandbox_mode(monkeypatch):
    monkeypatch.setenv("SANDBOX_MODE", "macos")
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("missing")
    container = MagicMock()
    container.id = "container-id"
    container.short_id = "abc123"
    client.containers.run.return_value = container

    DockerRuntimeLauncher(client).launch(
        RuntimeLaunchSpec(
            task_id="task-12345678",
            workflow="platform-test",
            image="runtime:latest",
            environment={},
        )
    )

    kwargs = client.containers.run.call_args.kwargs
    assert kwargs["security_opt"] == ["seccomp=unconfined"]
    assert "cap_add" not in kwargs
    assert kwargs["environment"]["CLAUDE_SANDBOX_ENABLE_WEAKER_NESTED"] == "1"


def test_docker_launcher_uses_gha_sandbox_mode(monkeypatch):
    monkeypatch.setenv("SANDBOX_MODE", "gha")
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("missing")
    container = MagicMock()
    container.id = "container-id"
    container.short_id = "abc123"
    client.containers.run.return_value = container

    DockerRuntimeLauncher(client).launch(
        RuntimeLaunchSpec(
            task_id="task-12345678",
            workflow="platform-test",
            image="runtime:latest",
            environment={},
        )
    )

    kwargs = client.containers.run.call_args.kwargs
    assert kwargs["security_opt"] == ["seccomp=unconfined", "apparmor=unconfined"]
    assert kwargs["cap_add"] == ["SYS_ADMIN"]
    assert kwargs["environment"]["CLAUDE_SANDBOX_ENABLE_WEAKER_NESTED"] == "1"


def test_docker_launcher_uses_gvisor_runtime_without_native_relaxations(monkeypatch):
    monkeypatch.setenv("SANDBOX_MODE", "gvisor")
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("missing")
    container = MagicMock()
    container.id = "container-id"
    container.short_id = "abc123"
    client.containers.run.return_value = container

    DockerRuntimeLauncher(client).launch(
        RuntimeLaunchSpec(
            task_id="task-12345678",
            workflow="platform-test",
            image="runtime:latest",
            environment={},
        )
    )

    kwargs = client.containers.run.call_args.kwargs
    assert kwargs["runtime"] == "runsc"
    assert "security_opt" not in kwargs
    assert "cap_add" not in kwargs
    assert "CLAUDE_SANDBOX_ENABLE_WEAKER_NESTED" not in kwargs["environment"]


def test_docker_launcher_preserves_docker_desktop_host_routing(monkeypatch):
    monkeypatch.setattr("session_manager.runtime_launchers.sys.platform", "darwin")
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("missing")
    container = MagicMock()
    container.id = "container-id"
    container.short_id = "abc123"
    client.containers.run.return_value = container

    DockerRuntimeLauncher(client).launch(
        RuntimeLaunchSpec(
            task_id="task-12345678",
            workflow="platform-test",
            image="runtime:latest",
            environment={"A": "1"},
        )
    )

    assert "extra_hosts" not in client.containers.run.call_args.kwargs


def test_docker_launcher_mounts_workflow_bundle_instead_of_source_dirs():
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("missing")
    container = MagicMock()
    container.id = "container-id"
    container.short_id = "abc123"
    client.containers.run.return_value = container

    launcher = DockerRuntimeLauncher(client)
    launcher.launch(
        RuntimeLaunchSpec(
            task_id="task-12345678",
            workflow="platform-test",
            image="runtime:latest",
            environment={"A": "1"},
            plugin_dir="/workflows/platform-test",
            shared_dir="/shared",
            workflow_bundle_path="/bundles/platform-test",
            workflow_bundle_checksum="sha256:abc",
            memory_volume_name="agent-memory-platform-test",
            container_name="session-test",
        )
    )

    kwargs = client.containers.run.call_args.kwargs
    assert kwargs["volumes"] == {
        "/bundles/platform-test": {"bind": "/workflow-bundle", "mode": "ro"},
        "agent-memory-platform-test": {"bind": "/memory", "mode": "rw"},
    }
    assert kwargs["environment"]["WORKFLOW_BUNDLE_PATH"] == "/workflow-bundle"
    assert kwargs["environment"]["WORKFLOW_BUNDLE_URI"] == "file:///workflow-bundle"
    assert kwargs["environment"]["WORKFLOW_BUNDLE_CHECKSUM"] == "sha256:abc"


def test_runtime_launcher_defaults_to_docker(monkeypatch):
    from shared.lib.config import settings

    reset_runtime_launcher_for_tests()
    monkeypatch.setattr(settings, "runtime_launcher", "docker")
    monkeypatch.setattr("session_manager.runtime_launchers._get_docker_client", lambda: MagicMock())

    assert get_runtime_launcher().provider == "docker"
    reset_runtime_launcher_for_tests()
