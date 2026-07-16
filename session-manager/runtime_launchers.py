"""Runtime launchers for ephemeral agent sessions.

Docker remains the default local/on-prem launcher. Cloud Run Jobs and
Kubernetes are optional implementations selected by configuration so the rest
of the platform can stop depending directly on a Docker daemon.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Protocol

import docker
from docker.errors import DockerException

from shared.lib.config import settings

logger = logging.getLogger(__name__)

DOCKER_NETWORK = "ai-ops-network"
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def _runtime_seccomp_unconfined_enabled() -> bool:
    return os.environ.get("RUNTIME_SECCOMP_UNCONFINED", "").strip().lower() in _TRUE_ENV_VALUES


@dataclass(frozen=True)
class RuntimeLaunchSpec:
    task_id: str
    workflow: str
    image: str
    environment: dict[str, str]
    plugin_dir: str | None = None
    shared_dir: str | None = None
    workflow_bundle_path: str | None = None
    workflow_bundle_uri: str | None = None
    workflow_bundle_checksum: str | None = None
    memory_volume_name: str | None = None
    container_name: str | None = None
    timeout_sec: int | None = None


@dataclass(frozen=True)
class RuntimeHandle:
    id: str
    short_id: str
    provider: str
    task_id: str
    workflow: str
    status: str = "running"
    raw: Any = None

    def _require_raw(self) -> Any:
        if self.raw is None:
            raise AttributeError(
                f"RuntimeHandle for provider {self.provider!r} does not expose container-style methods"
            )
        return self.raw

    def wait(self, *args: Any, **kwargs: Any) -> Any:
        return self._require_raw().wait(*args, **kwargs)

    def logs(self, *args: Any, **kwargs: Any) -> Any:
        return self._require_raw().logs(*args, **kwargs)

    def remove(self, *args: Any, **kwargs: Any) -> Any:
        return self._require_raw().remove(*args, **kwargs)

    def kill(self, *args: Any, **kwargs: Any) -> Any:
        return self._require_raw().kill(*args, **kwargs)


@dataclass(frozen=True)
class RuntimeSessionStatus:
    id: str
    short_id: str
    provider: str
    task_id: str
    workflow: str
    status: str
    exit_code: int | None = None
    logs: str = ""
    raw: Any = None


class RuntimeLauncher(Protocol):
    provider: str

    def launch(self, spec: RuntimeLaunchSpec) -> RuntimeHandle: ...

    def list_sessions(self) -> list[RuntimeSessionStatus]: ...

    def cleanup_session(self, status: RuntimeSessionStatus) -> None: ...

    def cancel(self, runtime_id: str | None = None, *, task_id: str | None = None) -> bool: ...


_docker_client: docker.DockerClient | None = None


def _get_docker_client() -> docker.DockerClient:
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.from_env()
    return _docker_client


def _environment_with_bundle_contract(
    spec: RuntimeLaunchSpec, *, mounted_bundle_path: str | None = None
) -> dict[str, str]:
    environment = dict(spec.environment)
    bundle_path = mounted_bundle_path or spec.workflow_bundle_path
    if bundle_path:
        environment["WORKFLOW_BUNDLE_PATH"] = bundle_path
    if spec.workflow_bundle_uri:
        environment["WORKFLOW_BUNDLE_URI"] = spec.workflow_bundle_uri
    elif bundle_path:
        environment["WORKFLOW_BUNDLE_URI"] = f"file://{bundle_path}"
    if spec.workflow_bundle_checksum:
        environment["WORKFLOW_BUNDLE_CHECKSUM"] = spec.workflow_bundle_checksum
    return environment


class DockerRuntimeLauncher:
    provider = "docker"

    def __init__(self, client: docker.DockerClient | None = None) -> None:
        self.client = client or _get_docker_client()

    def _cleanup_stale_container_name(self, container_name: str) -> None:
        try:
            existing = self.client.containers.get(container_name)
        except docker.errors.NotFound:
            return

        status = getattr(existing, "status", None)
        if status == "running":
            raise DockerException(f"Container name {container_name} is already in use by a running container")

        logger.info("Removing stale container %s before retry", container_name)
        existing.remove(force=True)

    def launch(self, spec: RuntimeLaunchSpec) -> RuntimeHandle:
        volumes: dict[str, dict[str, str]] = {}
        mounted_bundle_path = "/workflow-bundle" if spec.workflow_bundle_path else None
        if spec.workflow_bundle_path:
            volumes[spec.workflow_bundle_path] = {"bind": mounted_bundle_path, "mode": "ro"}
        elif spec.plugin_dir:
            volumes[spec.plugin_dir] = {"bind": "/plugin-src", "mode": "ro"}
        if spec.shared_dir and not spec.workflow_bundle_path:
            volumes[spec.shared_dir] = {"bind": "/shared", "mode": "ro"}
        if spec.memory_volume_name:
            volumes[spec.memory_volume_name] = {"bind": "/memory", "mode": "rw"}

        container_name = spec.container_name or f"session-{spec.task_id[:8]}-{spec.workflow}"
        self._cleanup_stale_container_name(container_name)
        run_kwargs = {
            "image": spec.image,
            "environment": _environment_with_bundle_contract(spec, mounted_bundle_path=mounted_bundle_path),
            "volumes": volumes,
            "network": DOCKER_NETWORK,
            "detach": True,
            "name": container_name,
            "labels": {
                "agentic_ops.task_id": spec.task_id,
                "agentic_ops.workflow": spec.workflow,
                "agentic_ops.type": "agent-session",
                "agentic_ops.runtime_provider": self.provider,
            },
        }
        if sys.platform == "linux":
            run_kwargs["extra_hosts"] = {"host.docker.internal": "host-gateway"}
        if _runtime_seccomp_unconfined_enabled():
            run_kwargs["security_opt"] = ["seccomp=unconfined"]
            run_kwargs["cap_add"] = ["SYS_ADMIN"]
            run_kwargs["environment"]["CLAUDE_SANDBOX_ENABLE_WEAKER_NESTED"] = "1"

        container = self.client.containers.run(**run_kwargs)
        return RuntimeHandle(
            id=container.id,
            short_id=container.short_id,
            provider=self.provider,
            task_id=spec.task_id,
            workflow=spec.workflow,
            raw=container,
        )

    def list_sessions(self) -> list[RuntimeSessionStatus]:
        containers = self.client.containers.list(all=True, filters={"label": "agentic_ops.type=agent-session"})
        statuses: list[RuntimeSessionStatus] = []
        for container in containers:
            task_id = container.labels.get("agentic_ops.task_id", "")
            workflow = container.labels.get("agentic_ops.workflow", "")
            exit_code = None
            logs = ""
            if container.status == "exited":
                exit_code = container.attrs.get("State", {}).get("ExitCode", -1)
                logs = container.logs(tail=200).decode("utf-8", errors="replace")
            statuses.append(
                RuntimeSessionStatus(
                    id=container.id,
                    short_id=container.short_id,
                    provider=self.provider,
                    task_id=task_id,
                    workflow=workflow,
                    status=container.status,
                    exit_code=exit_code,
                    logs=logs,
                    raw=container,
                )
            )
        return statuses

    def cleanup_session(self, status: RuntimeSessionStatus) -> None:
        container = status.raw or self.client.containers.get(status.id)
        container.remove(force=True)

    def cancel(self, runtime_id: str | None = None, *, task_id: str | None = None) -> bool:
        containers = []
        if runtime_id:
            try:
                containers = [self.client.containers.get(runtime_id)]
            except docker.errors.NotFound:
                return False
        elif task_id:
            containers = self.client.containers.list(all=True, filters={"label": f"agentic_ops.task_id={task_id}"})
        else:
            return False

        removed = False
        for container in containers:
            if container.status == "running":
                container.stop(timeout=5)
            container.remove(force=True)
            removed = True
        return removed


@dataclass
class _CloudRunExecutionRecord:
    execution_name: str
    task_id: str
    workflow: str


class CloudRunJobsLauncher:
    provider = "cloud_run_jobs"

    def __init__(self) -> None:
        if not settings.cloud_run_project or not settings.cloud_run_region or not settings.cloud_run_job_name:
            raise RuntimeError(
                "Cloud Run launcher requires CLOUD_RUN_PROJECT, CLOUD_RUN_REGION, and CLOUD_RUN_JOB_NAME"
            )
        from google.cloud import run_v2  # type: ignore[import-not-found]

        self.run_v2 = run_v2
        self.jobs_client = run_v2.JobsClient()
        self.executions_client = run_v2.ExecutionsClient()
        self.job_name = (
            f"projects/{settings.cloud_run_project}/locations/{settings.cloud_run_region}"
            f"/jobs/{settings.cloud_run_job_name}"
        )
        self._executions: dict[str, _CloudRunExecutionRecord] = {}

    def _execution_name_from_operation(self, operation: Any, spec: RuntimeLaunchSpec) -> str:
        metadata = getattr(operation, "metadata", None)
        for attr in ("name", "execution", "execution_name"):
            value = getattr(metadata, attr, None) if metadata is not None else None
            if value:
                return str(value)
        raw_operation = getattr(operation, "operation", None)
        value = getattr(raw_operation, "name", None)
        if value:
            return str(value)
        return f"{self.job_name}/executions/task-{spec.task_id}"

    def launch(self, spec: RuntimeLaunchSpec) -> RuntimeHandle:
        environment = _environment_with_bundle_contract(spec)
        env_overrides = [self.run_v2.EnvVar(name=name, value=value) for name, value in sorted(environment.items())]
        container_override = self.run_v2.RunJobRequest.Overrides.ContainerOverride(
            name="agent-runtime",
            env=env_overrides,
        )
        request = self.run_v2.RunJobRequest(
            name=self.job_name,
            overrides=self.run_v2.RunJobRequest.Overrides(container_overrides=[container_override]),
        )
        operation = self.jobs_client.run_job(request=request)
        execution_name = self._execution_name_from_operation(operation, spec)
        self._executions[execution_name] = _CloudRunExecutionRecord(execution_name, spec.task_id, spec.workflow)
        return RuntimeHandle(
            id=execution_name,
            short_id=execution_name.rsplit("/", 1)[-1][:12],
            provider=self.provider,
            task_id=spec.task_id,
            workflow=spec.workflow,
        )

    def _status_for_execution(self, execution_name: str) -> tuple[str, int | None]:
        execution = self.executions_client.get_execution(name=execution_name)
        terminal = getattr(execution, "terminal_condition", None)
        state = str(getattr(terminal, "state", "") or "").lower()
        reason = str(getattr(terminal, "reason", "") or "").lower()
        if "succeeded" in reason or "true" in state:
            return "exited", 0
        if "failed" in reason or "false" in state:
            return "exited", 1
        return "running", None

    def list_sessions(self) -> list[RuntimeSessionStatus]:
        statuses: list[RuntimeSessionStatus] = []
        for execution_name, record in list(self._executions.items()):
            try:
                status, exit_code = self._status_for_execution(execution_name)
            except Exception:
                logger.exception("Failed to read Cloud Run execution status for %s", execution_name)
                continue
            statuses.append(
                RuntimeSessionStatus(
                    id=execution_name,
                    short_id=execution_name.rsplit("/", 1)[-1][:12],
                    provider=self.provider,
                    task_id=record.task_id,
                    workflow=record.workflow,
                    status=status,
                    exit_code=exit_code,
                    logs="Cloud Run logs are available in Cloud Logging.",
                )
            )
        return statuses

    def cleanup_session(self, status: RuntimeSessionStatus) -> None:
        self._executions.pop(status.id, None)

    def cancel(self, runtime_id: str | None = None, *, task_id: str | None = None) -> bool:
        targets = []
        if runtime_id:
            targets = [runtime_id]
        elif task_id:
            targets = [name for name, record in self._executions.items() if record.task_id == task_id]
        for execution_name in targets:
            self.executions_client.cancel_execution(name=execution_name)
            self._executions.pop(execution_name, None)
        return bool(targets)


class KubernetesRuntimeLauncher:
    provider = "kubernetes"

    def __init__(self) -> None:
        from kubernetes import client, config  # type: ignore[import-not-found]

        config.load_incluster_config()
        self.client = client
        self.batch = client.BatchV1Api()
        self.core = client.CoreV1Api()
        self.namespace = settings.kubernetes_namespace

    def launch(self, spec: RuntimeLaunchSpec) -> RuntimeHandle:
        job_name = (spec.container_name or f"session-{spec.task_id[:8]}-{spec.workflow}").replace("_", "-")[:63]
        environment = _environment_with_bundle_contract(spec)
        env = [self.client.V1EnvVar(name=name, value=value) for name, value in sorted(environment.items())]
        container = self.client.V1Container(name="agent-runtime", image=spec.image, env=env)
        pod_spec = self.client.V1PodSpec(restart_policy="Never", containers=[container])
        template = self.client.V1PodTemplateSpec(
            metadata=self.client.V1ObjectMeta(
                labels={"agentic_ops.task_id": spec.task_id, "agentic_ops.workflow": spec.workflow}
            ),
            spec=pod_spec,
        )
        job_spec = self.client.V1JobSpec(template=template, backoff_limit=0)
        job = self.client.V1Job(
            metadata=self.client.V1ObjectMeta(
                name=job_name,
                labels={
                    "agentic_ops.task_id": spec.task_id,
                    "agentic_ops.workflow": spec.workflow,
                    "agentic_ops.type": "agent-session",
                },
            ),
            spec=job_spec,
        )
        created = self.batch.create_namespaced_job(namespace=self.namespace, body=job)
        runtime_id = created.metadata.name
        return RuntimeHandle(runtime_id, runtime_id[:12], self.provider, spec.task_id, spec.workflow)

    def list_sessions(self) -> list[RuntimeSessionStatus]:
        jobs = self.batch.list_namespaced_job(namespace=self.namespace, label_selector="agentic_ops.type=agent-session")
        statuses: list[RuntimeSessionStatus] = []
        for job in jobs.items:
            labels = job.metadata.labels or {}
            succeeded = int(job.status.succeeded or 0)
            failed = int(job.status.failed or 0)
            state = "exited" if succeeded or failed else "running"
            exit_code = 0 if succeeded else (1 if failed else None)
            statuses.append(
                RuntimeSessionStatus(
                    id=job.metadata.name,
                    short_id=job.metadata.name[:12],
                    provider=self.provider,
                    task_id=str(labels.get("agentic_ops.task_id") or ""),
                    workflow=str(labels.get("agentic_ops.workflow") or ""),
                    status=state,
                    exit_code=exit_code,
                    logs="Kubernetes pod logs are available via kubectl/log aggregation.",
                    raw=job,
                )
            )
        return statuses

    def cleanup_session(self, status: RuntimeSessionStatus) -> None:
        self.batch.delete_namespaced_job(
            name=status.id,
            namespace=self.namespace,
            propagation_policy="Background",
        )

    def cancel(self, runtime_id: str | None = None, *, task_id: str | None = None) -> bool:
        if runtime_id:
            self.cleanup_session(
                RuntimeSessionStatus(runtime_id, runtime_id[:12], self.provider, task_id or "", "", "running")
            )
            return True
        if not task_id:
            return False
        removed = False
        for status in self.list_sessions():
            if status.task_id == task_id:
                self.cleanup_session(status)
                removed = True
        return removed


_launcher: RuntimeLauncher | None = None


def get_runtime_launcher() -> RuntimeLauncher:
    global _launcher
    if _launcher is not None:
        return _launcher

    launcher = settings.runtime_launcher.strip().lower()
    if launcher in {"", "docker"}:
        _launcher = DockerRuntimeLauncher()
    elif launcher in {"cloud_run", "cloud_run_jobs", "gcp"}:
        _launcher = CloudRunJobsLauncher()
    elif launcher in {"kubernetes", "k8s"}:
        _launcher = KubernetesRuntimeLauncher()
    else:
        raise RuntimeError(f"Unsupported runtime launcher: {settings.runtime_launcher}")
    return _launcher


def reset_runtime_launcher_for_tests() -> None:
    global _launcher
    _launcher = None
