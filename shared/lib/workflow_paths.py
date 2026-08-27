"""Workflow repository/path resolution helpers.

The public platform loads workflows from configured workflow repositories or
mounted workflow roots. Core images do not contain authored workflow packages.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

from shared.lib.config import settings
from shared.lib.github_app import github_git_auth_environment, github_installation_token
from shared.lib.platform_secrets import load_workflow_repo_github_connection

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkflowPackage:
    name: str
    path: Path
    agent_yaml_path: Path
    config: dict
    raw_yaml: str


def _split_path_list(value: str) -> list[Path]:
    return [Path(part).expanduser() for part in value.split(os.pathsep) if part.strip()]


def _sync_configured_workflow_repo(*, ref_override: str | None = None, raise_on_error: bool = False) -> Path | None:
    """Clone/fetch an optional external workflow repo and return its local path.

    By default this is deliberately conservative: missing git or network
    errors are logged and do not prevent local/mounted workflow roots from
    being scanned. Pass ``raise_on_error=True`` (used by the explicit
    workflow-repo sync pipeline) to surface failures instead of swallowing
    them.
    """
    repo_url = settings.workflow_repo_url.strip()
    if not repo_url or settings.workflow_repo_source.strip().lower() == "local":
        return None

    git_binary = shutil.which("git")
    if not git_binary:
        logger.warning("Skipping workflow repo sync because git is not installed")
        return None

    ref = (ref_override if ref_override is not None else settings.workflow_repo_ref).strip()
    platform_config_file = settings.platform_config_file
    connection_name = load_workflow_repo_github_connection(platform_config_file)
    token = (
        github_installation_token(
            connection_name,
            platform_config_file=platform_config_file,
            age_identity=settings.age_identity or None,
        )
        if connection_name
        else ""
    )
    local_path = Path(settings.workflow_repo_local_path).expanduser()
    try:
        with github_git_auth_environment(token) as git_environment:
            if not local_path.exists():
                local_path.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(  # noqa: S603 - operator-configured workflow repo sync command.
                    [git_binary, "clone", repo_url, str(local_path)],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=dict(git_environment),
                )
            else:
                subprocess.run(  # noqa: S603 - operator-configured workflow repo sync command.
                    [git_binary, "-C", str(local_path), "remote", "set-url", "origin", repo_url],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=dict(git_environment),
                )
                subprocess.run(  # noqa: S603 - operator-configured workflow repo sync command.
                    [git_binary, "-C", str(local_path), "fetch", "--all", "--prune"],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=dict(git_environment),
                )

            if ref:
                subprocess.run(  # noqa: S603 - operator-configured workflow repo sync command.
                    [git_binary, "-C", str(local_path), "checkout", ref],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=dict(git_environment),
                )
        return local_path
    except (OSError, subprocess.CalledProcessError) as exc:
        if raise_on_error:
            raise RuntimeError(f"Failed to sync workflow repo {repo_url} into {local_path}: {exc}") from exc
        logger.warning("Failed to sync workflow repo %s into %s: %s", repo_url, local_path, exc)
        return None


def sync_workflow_repo_to_ref(ref: str | None) -> Path | None:
    """Sync the configured workflow repo to an explicit ref, raising on failure.

    Used by the workflow-repo sync pipeline (operator-triggered or pinned
    version changes), as opposed to the conservative best-effort sync used
    on every workflow-discovery call.
    """
    return _sync_configured_workflow_repo(ref_override=ref, raise_on_error=True)


def configured_workflow_roots() -> list[Path]:
    """Return configured workflow search roots in precedence order."""
    roots: list[Path] = []

    synced = _sync_configured_workflow_repo()
    if synced is not None:
        roots.append(synced)

    roots.extend(_split_path_list(settings.workflow_repo_paths))
    if settings.workflow_root:
        roots.append(Path(settings.workflow_root).expanduser())

    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root.resolve()) if root.exists() else str(root)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(root)
    return deduped


def _workflow_dirs_for_root(root: Path) -> Iterable[Path]:
    """Yield workflow package directories for a root.

    A root may be:
    - a direct workflow directory containing agent.yaml
    - a repo root containing workflows/*/agent.yaml
    - a workflow root containing */agent.yaml
    """
    if not root.exists():
        return []

    workflow_dirs: list[Path] = []
    if (root / "agent.yaml").exists():
        workflow_dirs.append(root)

    workflows_root = root / "workflows"
    if workflows_root.exists():
        workflow_dirs.extend(path.parent for path in sorted(workflows_root.glob("*/agent.yaml")))

    workflow_dirs.extend(path.parent for path in sorted(root.glob("*/agent.yaml")))

    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in workflow_dirs:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(path)
    return deduped


def discover_workflow_packages(roots: Iterable[Path] | None = None) -> list[WorkflowPackage]:
    packages: list[WorkflowPackage] = []
    seen_names: set[str] = set()

    for root in roots or configured_workflow_roots():
        if not root.exists():
            logger.warning("Workflow root not found: %s", root)
            continue
        for workflow_dir in _workflow_dirs_for_root(root):
            agent_yaml_path = workflow_dir / "agent.yaml"
            try:
                raw_yaml = agent_yaml_path.read_text(encoding="utf-8")
                config = yaml.safe_load(raw_yaml) or {}
            except (OSError, yaml.YAMLError):
                logger.exception("Failed to parse workflow config: %s", agent_yaml_path)
                continue
            if not isinstance(config, dict):
                logger.warning("Workflow config must be a mapping: %s", agent_yaml_path)
                continue

            name = str(config.get("name") or workflow_dir.name)
            if name in seen_names:
                raise ValueError(f"Duplicate workflow name {name!r} discovered at {workflow_dir}")
            seen_names.add(name)
            packages.append(
                WorkflowPackage(
                    name=name,
                    path=workflow_dir,
                    agent_yaml_path=agent_yaml_path,
                    config=config,
                    raw_yaml=raw_yaml,
                )
            )
    return packages


def find_workflow_package(workflow: str) -> WorkflowPackage | None:
    for package in discover_workflow_packages():
        if package.name == workflow or package.path.name == workflow:
            return package
    return None


def workflow_repo_path(package: WorkflowPackage) -> str:
    """Return the stored package path for control-plane display and file reads."""
    return str(package.path)
