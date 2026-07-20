"""Runtime launchers for ephemeral agent sessions.

Docker remains the default local/on-prem launcher. Kubernetes is an optional
implementation selected by configuration so the rest of the platform can stop
depending directly on a Docker daemon.
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

DEFAULT_DOCKER_NETWORK = "ai-ops-network"


def _sandbox_mode() -> str:
    return os.environ.get("SANDBOX_MODE", "").strip().lower()


def _docker_network() -> str:
    return os.environ.get("DOCKER_NETWORK", DEFAULT_DOCKER_NETWORK).strip() or DEFAULT_DOCKER_NETWORK


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
            "network": _docker_network(),
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
        sandbox_mode = _sandbox_mode()
        if sandbox_mode == "macos":
            run_kwargs["security_opt"] = ["seccomp=unconfined"]
            run_kwargs["environment"]["CLAUDE_SANDBOX_ENABLE_WEAKER_NESTED"] = "1"
        elif sandbox_mode == "gha":
            run_kwargs["security_opt"] = ["seccomp=unconfined", "apparmor=unconfined"]
            run_kwargs["cap_add"] = ["SYS_ADMIN"]
            run_kwargs["environment"]["CLAUDE_SANDBOX_ENABLE_WEAKER_NESTED"] = "1"
        elif sandbox_mode == "gvisor":
            run_kwargs["runtime"] = "runsc"

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
        init_containers = []
        volumes = []
        if spec.memory_volume_name:
            memory_mount = self.client.V1VolumeMount(name="agent-memory", mount_path="/memory")
            container.volume_mounts = [memory_mount]
            helper_image = settings.kubernetes_memory_helper_image.strip()
            if not helper_image:
                raise RuntimeError("Kubernetes launcher requires KUBERNETES_MEMORY_HELPER_IMAGE for agent memory sync")
            storage_env = [
                self.client.V1EnvVar(name="OBJECT_STORE_PROVIDER", value=settings.object_store_provider),
                self.client.V1EnvVar(name="OBJECT_STORE_ENDPOINT", value=settings.object_store_endpoint),
                self.client.V1EnvVar(name="OBJECT_STORE_ACCESS_KEY", value=settings.object_store_access_key),
                self.client.V1EnvVar(name="OBJECT_STORE_SECURE", value=str(settings.object_store_secure).lower()),
                self.client.V1EnvVar(name="OBJECT_STORE_GCP_PROJECT", value=settings.object_store_gcp_project),
            ]
            if settings.kubernetes_bootstrap_secret:
                storage_env.append(
                    self.client.V1EnvVar(
                        name="OBJECT_STORE_SECRET_KEY",
                        value_from=self.client.V1EnvVarSource(
                            secret_key_ref=self.client.V1SecretKeySelector(
                                name=settings.kubernetes_bootstrap_secret,
                                key="OBJECT_STORE_SECRET_KEY",
                                optional=True,
                            )
                        ),
                    )
                )
            elif settings.object_store_secret_key:
                storage_env.append(
                    self.client.V1EnvVar(name="OBJECT_STORE_SECRET_KEY", value=settings.object_store_secret_key)
                )
            init_containers = [
                self.client.V1Container(
                    name="memory-restore",
                    image=helper_image,
                    command=["python", "-m", "session_manager.memory_sync", "restore", spec.workflow, "/memory"],
                    env=storage_env,
                    volume_mounts=[memory_mount],
                )
            ]
            sync_container = self.client.V1Container(
                name="memory-upload",
                image=helper_image,
                command=["python", "-m", "session_manager.memory_sync", "wait-upload", spec.workflow, "/memory"],
                env=storage_env,
                volume_mounts=[memory_mount],
            )
            container.env.append(self.client.V1EnvVar(name="KUBERNETES_MEMORY_SYNC", value="1"))
            volumes = [self.client.V1Volume(name="agent-memory", empty_dir=self.client.V1EmptyDirVolumeSource())]
            containers = [container, sync_container]
        else:
            containers = [container]
        pod_spec = self.client.V1PodSpec(
            restart_policy="Never",
            containers=containers,
            init_containers=init_containers,
            volumes=volumes,
        )
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
    elif launcher in {"kubernetes", "k8s"}:
        _launcher = KubernetesRuntimeLauncher()
    else:
        raise RuntimeError(f"Unsupported runtime launcher: {settings.runtime_launcher}")
    return _launcher


def reset_runtime_launcher_for_tests() -> None:
    global _launcher
    _launcher = None
