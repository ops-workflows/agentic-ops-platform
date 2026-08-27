#!/usr/bin/env python3
"""Guided bootstrap for the operator/infra config layer.

Agentic Ops config has three layers (see docs/roadmap for the full picture):

1. Bootstrap / infra (this script) — the minimum needed to cold-start and
    reach the workflow repo: its URL/ref, an optional one-time clone PAT, the
    age identity used to decrypt its secrets, and the model gateway API key.
    Operator-owned, generated, never committed; the PAT is never generated.
2. Instance config — the workflow repo's own `platform-config.yaml` (message
   bus, mcps, connectors, memory banks, model profiles, workflow secrets).
   Read only after the repo has been fetched using layer 1.
3. Workflow packages — `workflows/`, `skills/`, `hooks/`, custom `mcps/`,
   `connectors/` inside the workflow repo.

This script produces layer 1 and, for Kubernetes, publishes the checkout's
unchanged `platform-config.yaml` as a startup Secret.

Run through `make bootstrap`. The script always prompts for the operator-owned
bootstrap values and writes the standard artifact for the selected target.
"""

from __future__ import annotations

import getpass
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from shared.lib.github_app import github_git_auth_environment

REPO_ROOT = Path(__file__).resolve().parents[1]

VALID_TARGETS = ("compose", "kubernetes")
VALID_SOURCES = ("remote", "local")


@dataclass(frozen=True)
class BootstrapConfig:
    target: str
    source: str
    repo_url: str = ""
    repo_ref: str = ""
    repo_pat: str = ""
    local_path: str = ""
    age_identity: str = ""
    llm_api_key: str = ""
    pg_password: str = ""
    object_store_secret_key: str = ""
    namespace: str = "default"

    def validate(self) -> None:
        if self.target not in VALID_TARGETS:
            raise ValueError(f"Deployment target must be one of {VALID_TARGETS}, got {self.target!r}")
        if self.source not in VALID_SOURCES:
            raise ValueError(f"Workflow source must be one of {VALID_SOURCES}, got {self.source!r}")
        if self.source == "remote" and not self.repo_url:
            raise ValueError("Workflow repo URL is required for a remote source")
        if not self.local_path:
            raise ValueError("Local workflow-repo checkout path is required")
        if not self.age_identity:
            raise ValueError("AGE identity is required")
        if not self.llm_api_key:
            raise ValueError("LLM API key is required")
        if not self.pg_password:
            raise ValueError("Postgres password is required")
        if not self.object_store_secret_key:
            raise ValueError("Object-store secret key is required")


def normalize_age_identity(value: str) -> str:
    """Return the AGE_IDENTITY value in the runtime's expected form.

    Accepts a raw armored key, an existing `file:`-prefixed path, or a bare
    filesystem path. A path is read into the generated bootstrap artifact:
    a host filesystem path cannot be resolved inside a Compose or Kubernetes
    container.
    """
    value = value.strip()
    if not value or value.startswith("AGE-SECRET-KEY-"):
        return value
    path_value = value.removeprefix("file:")
    key_path = Path(path_value).expanduser()
    if key_path.is_file():
        return key_path.read_text(encoding="utf-8").strip()
    return value


def build_bootstrap_env(config: BootstrapConfig) -> dict[str, str]:
    """Return the bootstrap KEY=VALUE env mapping for the chosen target/source."""
    env: dict[str, str] = {
        "AGE_IDENTITY": normalize_age_identity(config.age_identity),
        "LLM_API_KEY": config.llm_api_key,
        "PG_PASSWORD": config.pg_password,
        "OBJECT_STORE_SECRET_KEY": config.object_store_secret_key,
        "WORKFLOW_REPO_SOURCE": "local" if config.target == "compose" else config.source,
    }
    if config.source == "remote":
        env["WORKFLOW_REPO_URL"] = config.repo_url
        env["WORKFLOW_REPO_REF"] = config.repo_ref
        if config.target == "compose":
            local_path = str(Path(config.local_path).expanduser())
            env["HOST_WORKFLOW_REPO_PATH"] = local_path
            env["HOST_PLATFORM_CONFIG_FILE"] = str(Path(local_path) / "platform-config.yaml")
            env["WORKFLOW_COMPOSE_ENV_FILE"] = str(Path(local_path) / "deploy" / "compose.env")
            env["WORKFLOW_COMPOSE_OVERRIDE_FILE"] = str(Path(local_path) / "deploy" / "docker-compose.override.yml")
    else:
        local_path = str(Path(config.local_path).expanduser())
        if config.repo_url:
            env["WORKFLOW_REPO_URL"] = config.repo_url
        if config.target == "compose":
            env["HOST_WORKFLOW_REPO_PATH"] = local_path
            env["HOST_PLATFORM_CONFIG_FILE"] = str(Path(local_path) / "platform-config.yaml")
            env["WORKFLOW_COMPOSE_ENV_FILE"] = str(Path(local_path) / "deploy" / "compose.env")
            env["WORKFLOW_COMPOSE_OVERRIDE_FILE"] = str(Path(local_path) / "deploy" / "docker-compose.override.yml")
        else:
            env["WORKFLOW_REPO_PATHS"] = local_path
    if config.target == "kubernetes":
        env["KUBERNETES_NAMESPACE"] = config.namespace
    return env


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def render_compose_env(env: dict[str, str]) -> str:
    lines = [
        "# Generated by scripts/bootstrap.py -- do not commit.",
        "# Operator/bootstrap secrets only. Instance config lives in the workflow",
        "# repo's platform-config.yaml.",
        *(f"{key}={value}" for key, value in env.items()),
    ]
    return "\n".join(lines) + "\n"


def render_k8s_secret_script(
    env: dict[str, str],
    *,
    secret_name: str = "agentic-ops-bootstrap",  # noqa: S107 - resource name, not a credential
    platform_config_file: str = "",
    platform_config_secret_name: str = "agentic-ops-platform-config",  # noqa: S107
    namespace: str = "default",
) -> str:
    literals = " \\\n  ".join(f"--from-literal={key}={_shell_quote(value)}" for key, value in env.items())
    script = (
        "#!/usr/bin/env bash\n"
        "# Generated by scripts/bootstrap.py -- do not commit. Run once per cluster/namespace.\n"
        "set -euo pipefail\n\n"
        f"kubectl create secret generic {secret_name} \\\n"
        f"  --namespace {namespace} \\\n"
        f"  {literals} \\\n"
        "  --dry-run=client -o yaml | kubectl apply -f -\n"
    )
    if platform_config_file:
        script += (
            "\n"
            f"kubectl create secret generic {platform_config_secret_name} \\\n"
            f"  --namespace {namespace} \\\n"
            f"  --from-file=platform-config.yaml={_shell_quote(platform_config_file)} \\\n"
            "  --dry-run=client -o yaml | kubectl apply -f -\n"
        )
    return script


def prepare_workflow_repo(config: BootstrapConfig) -> Path:
    """Clone or refresh the initial checkout without persisting its bootstrap PAT."""
    checkout = Path(config.local_path).expanduser()
    if config.source == "local":
        return checkout
    git_binary = shutil.which("git")
    if not git_binary:
        raise RuntimeError("git is required to clone the workflow repository")
    checkout.parent.mkdir(parents=True, exist_ok=True)
    with github_git_auth_environment(config.repo_pat) as git_environment:
        if (checkout / ".git").is_dir():
            commands = [
                [git_binary, "-C", str(checkout), "remote", "set-url", "origin", config.repo_url],
                [git_binary, "-C", str(checkout), "fetch", "--all", "--prune"],
            ]
        elif checkout.exists() and any(checkout.iterdir()):
            raise RuntimeError(f"Workflow repository checkout is not empty: {checkout}")
        else:
            commands = [[git_binary, "clone", config.repo_url, str(checkout)]]
        if config.repo_ref:
            commands.append([git_binary, "-C", str(checkout), "checkout", config.repo_ref])
        for command in commands:
            subprocess.run(  # noqa: S603 - operator-provided repository and validated command shape.
                command,
                check=True,
                env=dict(git_environment),
            )
    return checkout


def write_artifact(config: BootstrapConfig, *, output_dir: Path = REPO_ROOT) -> Path:
    env = build_bootstrap_env(config)
    if config.target == "compose":
        path = output_dir / "compose.env"
        path.write_text(render_compose_env(env), encoding="utf-8")
        return path

    path = output_dir / "dist" / "bootstrap" / "k8s-secret.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = render_k8s_secret_script(
        env,
        namespace=config.namespace,
        platform_config_file=str(Path(config.local_path).expanduser() / "platform-config.yaml"),
    )
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _prompt(label: str, *, default: str = "", secret: bool = False) -> str:
    reader = getpass.getpass if secret else input
    suffix = f" [{default}]" if default and not secret else ""
    value = reader(f"{label}{suffix}: ").strip()
    return value or default


def gather_config_interactively() -> BootstrapConfig:
    target = _prompt("Deployment target (compose/kubernetes)", default="compose")
    default_source = "local" if target == "compose" else "remote"
    source = _prompt("Workflow source (remote/local)", default=default_source)

    repo_url = ""
    repo_ref = ""
    repo_pat = ""
    local_path = ""
    if source == "remote":
        repo_url = _prompt("Workflow repo URL")
        repo_ref = _prompt("Workflow repo ref (tag or SHA)", default="main")
        local_path = _prompt("Initial local checkout path")
        repo_pat = _prompt("One-time clone PAT (blank for public repos; never stored)", secret=True)
    elif source == "local":
        local_path = _prompt("Local workflow-repo checkout path")
        repo_url = _prompt("Workflow GitHub URL (for version lookup and reflection PRs; blank to disable)")

    namespace = _prompt("Kubernetes namespace", default="default") if target == "kubernetes" else "default"

    age_identity = _prompt("AGE identity (inline key or path to key.txt)", secret=True)
    llm_api_key = _prompt("LLM API key", secret=True)
    pg_password = _prompt("Postgres password", secret=True)
    object_store_secret_key = _prompt("Object-store secret key", secret=True)

    return BootstrapConfig(
        target=target,
        source=source,
        repo_url=repo_url,
        repo_ref=repo_ref,
        repo_pat=repo_pat,
        local_path=local_path,
        age_identity=age_identity,
        llm_api_key=llm_api_key,
        pg_password=pg_password,
        object_store_secret_key=object_store_secret_key,
        namespace=namespace,
    )


def main() -> int:
    config = gather_config_interactively()

    try:
        config.validate()
        prepare_workflow_repo(config)
    except (OSError, RuntimeError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    path = write_artifact(config)
    print(f"Wrote bootstrap artifact: {path}")
    if config.target == "compose":
        print("Run: make up")
    else:
        print(
            f"Run {path} to create the bootstrap and platform-config secrets, then set "
            "platformConfig.existingSecret=agentic-ops-platform-config and deploy."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
