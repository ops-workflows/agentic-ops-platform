from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import docker.errors
import pytest
from session_manager.runtime_launchers import (
    CloudRunJobsLauncher,
    DockerRuntimeLauncher,
    RuntimeLaunchSpec,
    get_runtime_launcher,
    reset_runtime_launcher_for_tests,
)

pytestmark = pytest.mark.unit


def test_docker_launcher_translates_spec_to_container_run(monkeypatch):
    monkeypatch.setattr("session_manager.runtime_launchers.sys.platform", "linux")
    monkeypatch.delenv("RUNTIME_SECCOMP_UNCONFINED", raising=False)
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
    assert kwargs["extra_hosts"] == {"host.docker.internal": "host-gateway"}
    assert kwargs["labels"]["agentic_ops.runtime_provider"] == "docker"
    assert "security_opt" not in kwargs


def test_docker_launcher_allows_opt_in_unconfined_seccomp(monkeypatch):
    monkeypatch.setenv("RUNTIME_SECCOMP_UNCONFINED", "true")
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

    assert client.containers.run.call_args.kwargs["security_opt"] == ["seccomp=unconfined", "apparmor=unconfined"]
    assert client.containers.run.call_args.kwargs["cap_add"] == ["SYS_ADMIN"]
    assert client.containers.run.call_args.kwargs["environment"]["CLAUDE_SANDBOX_ENABLE_WEAKER_NESTED"] == "1"


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


def test_cloud_run_launcher_builds_env_override_request(monkeypatch):
    from shared.lib.config import settings

    class FakeEnvVar:
        def __init__(self, *, name, value):
            self.name = name
            self.value = value

    class FakeContainerOverride:
        def __init__(self, *, name, env):
            self.name = name
            self.env = env

    class FakeOverrides:
        ContainerOverride = FakeContainerOverride

        def __init__(self, *, container_overrides):
            self.container_overrides = container_overrides

    class FakeRunJobRequest:
        Overrides = FakeOverrides

        def __init__(self, *, name, overrides):
            self.name = name
            self.overrides = overrides

    class FakeJobsClient:
        last_request = None

        def run_job(self, *, request):
            FakeJobsClient.last_request = request
            return SimpleNamespace(metadata=SimpleNamespace(name="executions/task-123"))

    fake_run_v2 = types.ModuleType("google.cloud.run_v2")
    fake_run_v2.EnvVar = FakeEnvVar
    fake_run_v2.RunJobRequest = FakeRunJobRequest
    fake_run_v2.JobsClient = FakeJobsClient
    fake_run_v2.ExecutionsClient = lambda: MagicMock()

    google_module = types.ModuleType("google")
    cloud_module = types.ModuleType("google.cloud")
    cloud_module.run_v2 = fake_run_v2
    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud_module)
    monkeypatch.setitem(sys.modules, "google.cloud.run_v2", fake_run_v2)
    monkeypatch.setattr(settings, "cloud_run_project", "proj")
    monkeypatch.setattr(settings, "cloud_run_region", "region")
    monkeypatch.setattr(settings, "cloud_run_job_name", "runtime-job")

    launcher = CloudRunJobsLauncher()
    handle = launcher.launch(
        RuntimeLaunchSpec(
            task_id="task-123",
            workflow="wf",
            image="ignored-by-existing-job",
            environment={"B": "2", "A": "1"},
        )
    )

    assert handle.id == "executions/task-123"
    request = FakeJobsClient.last_request
    assert request.name == "projects/proj/locations/region/jobs/runtime-job"
    override = request.overrides.container_overrides[0]
    assert override.name == "agent-runtime"
    assert [(env.name, env.value) for env in override.env] == [("A", "1"), ("B", "2")]
