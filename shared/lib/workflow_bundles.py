"""Workflow bundle assembly helpers."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from shared.lib.workflow_paths import discover_workflow_packages

BUNDLE_VERSION = 1
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".runtime-mount", "node_modules", ".next"}
SKIP_FILES = {".DS_Store"}
SECRET_KEY_MARKERS = ("secret", "token", "password", "api_key", "apikey", "private_key")
WORKFLOW_TOP_LEVEL_FILES = ("agent.yaml", "settings.json", ".mcp.json", "README.md", "CLAUDE.md")
WORKFLOW_TOP_LEVEL_DIRS = ("agents", "skills", "hooks")


@dataclass(frozen=True)
class WorkflowRepoMetadata:
    name: str = "local"
    url: str = ""
    ref: str = ""
    commit: str = ""


@dataclass(frozen=True)
class BundleBuildResult:
    workflow: str
    bundle_dir: Path
    manifest_path: Path
    manifest: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


def build_workflow_bundle(
    *,
    workflow: str,
    output_dir: Path,
    platform_root: Path,
    workflow_roots: list[Path],
    repo_metadata: WorkflowRepoMetadata | None = None,
    created_at: datetime | None = None,
) -> BundleBuildResult:
    """Assemble one workflow package into a runtime bundle directory."""
    packages = discover_workflow_packages(workflow_roots)
    package = next((item for item in packages if item.name == workflow or item.path.name == workflow), None)
    if package is None:
        known = ", ".join(sorted(item.name for item in packages)) or "none"
        raise ValueError(f"Workflow {workflow!r} was not found. Discovered workflows: {known}")

    bundle_dir = output_dir / package.name
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    team_root = _team_root_for_package(package.path)

    core_hash = _hash_sources([platform_root / "CLAUDE.md", platform_root / "skills", platform_root / "hooks"])
    team_hash = _hash_sources([team_root / "CLAUDE.md", team_root / "skills", team_root / "hooks"])
    workflow_hash = _hash_sources([package.path])

    shadowed_assets: list[dict[str, str]] = []
    asset_sources: dict[str, str] = {}
    _copy_file_if_exists(
        platform_root / "CLAUDE.md",
        bundle_dir / "CLAUDE.md",
        bundle_dir=bundle_dir,
        layer="core",
        shadowed_assets=shadowed_assets,
        asset_sources=asset_sources,
    )
    _overlay_dir(
        platform_root / "skills",
        bundle_dir / "skills",
        bundle_dir=bundle_dir,
        layer="core",
        shadowed_assets=shadowed_assets,
        asset_sources=asset_sources,
    )
    _overlay_dir(
        platform_root / "hooks",
        bundle_dir / "hooks",
        bundle_dir=bundle_dir,
        layer="core",
        shadowed_assets=shadowed_assets,
        asset_sources=asset_sources,
    )

    _copy_file_if_exists(
        team_root / "CLAUDE.md",
        bundle_dir / "CLAUDE.md",
        bundle_dir=bundle_dir,
        layer="team",
        shadowed_assets=shadowed_assets,
        asset_sources=asset_sources,
    )
    _overlay_dir(
        team_root / "skills",
        bundle_dir / "skills",
        bundle_dir=bundle_dir,
        layer="team",
        shadowed_assets=shadowed_assets,
        asset_sources=asset_sources,
    )
    _overlay_dir(
        team_root / "hooks",
        bundle_dir / "hooks",
        bundle_dir=bundle_dir,
        layer="team",
        shadowed_assets=shadowed_assets,
        asset_sources=asset_sources,
    )

    for file_name in WORKFLOW_TOP_LEVEL_FILES:
        _copy_file_if_exists(
            package.path / file_name,
            bundle_dir / file_name,
            bundle_dir=bundle_dir,
            layer="workflow",
            shadowed_assets=shadowed_assets,
            asset_sources=asset_sources,
        )
    for dir_name in WORKFLOW_TOP_LEVEL_DIRS:
        _overlay_dir(
            package.path / dir_name,
            bundle_dir / dir_name,
            bundle_dir=bundle_dir,
            layer="workflow",
            shadowed_assets=shadowed_assets,
            asset_sources=asset_sources,
        )

    warnings.extend(_validate_no_plaintext_secrets(bundle_dir))
    if warnings:
        shutil.rmtree(bundle_dir)
        raise ValueError("Workflow bundle validation failed:\n" + "\n".join(f"- {item}" for item in warnings))

    metadata = repo_metadata or WorkflowRepoMetadata(commit=git_commit_for_path(team_root))
    now = created_at or datetime.now(UTC)
    manifest = {
        "bundle_version": BUNDLE_VERSION,
        "platform_version": platform_version_for_root(platform_root),
        "workflow": {
            "name": package.name,
            "config_hash": _hash_bytes(package.raw_yaml.encode("utf-8")),
            "path": str(package.path),
        },
        "repo": {
            "name": metadata.name,
            "url": metadata.url,
            "ref": metadata.ref,
            "commit": metadata.commit or git_commit_for_path(team_root),
        },
        "assets": {
            "core_sha": core_hash,
            "team_sha": team_hash,
            "workflow_sha": workflow_hash,
            "sources": dict(sorted(asset_sources.items())),
            "shadowed": shadowed_assets,
        },
        "created_at": now.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    manifest_path = bundle_dir / "manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    return BundleBuildResult(
        workflow=package.name,
        bundle_dir=bundle_dir,
        manifest_path=manifest_path,
        manifest=manifest,
        warnings=warnings,
    )


def upload_bundle_archive(bundle_dir: Path, workflow: str, *, bucket: str, key_prefix: str = "bundles") -> str:
    """Tar a built bundle directory and upload it to object storage.

    Returns the object key (e.g. ``bundles/<workflow>.tar.gz``). Used so
    compose, Kubernetes, and GCP deployments can all consume bundles through
    the same object-storage-backed transport instead of a host bind mount.
    """
    import io
    import tarfile

    from shared.lib.object_store import upload_bytes

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(bundle_dir, arcname=".")

    key = f"{key_prefix}/{workflow}.tar.gz"
    upload_bytes(bucket, key, buffer.getvalue(), content_type="application/gzip")
    return key


def platform_version_for_root(platform_root: Path) -> str:
    pyproject = platform_root / "pyproject.toml"
    if not pyproject.exists():
        return "0.0.0"
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("version") and "=" in line:
            return line.split("=", 1)[1].strip().strip('"')
    return "0.0.0"


def _team_root_for_package(workflow_dir: Path) -> Path:
    if workflow_dir.parent.name == "workflows":
        return workflow_dir.parent.parent
    return workflow_dir.parent


def _copy_file_if_exists(
    src: Path,
    dest: Path,
    *,
    bundle_dir: Path,
    layer: str,
    shadowed_assets: list[dict[str, str]],
    asset_sources: dict[str, str],
) -> None:
    if not src.exists() or not src.is_file() or src.name in SKIP_FILES:
        return
    if dest.exists():
        shadowed_assets.append({"path": str(dest.relative_to(dest.parents[1])), "by": layer})
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    asset_sources[str(dest.relative_to(bundle_dir))] = layer


def _overlay_dir(
    src: Path,
    dest: Path,
    *,
    bundle_dir: Path,
    layer: str,
    shadowed_assets: list[dict[str, str]],
    asset_sources: dict[str, str],
) -> None:
    if not src.exists() or not src.is_dir():
        return
    for path in sorted(src.rglob("*")):
        if any(part in SKIP_DIRS for part in path.parts) or path.name in SKIP_FILES:
            continue
        if path.is_dir():
            continue
        relative = path.relative_to(src)
        target = dest / relative
        if target.exists():
            shadowed_assets.append({"path": str(target.relative_to(dest.parents[0])), "by": layer})
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        asset_sources[str(target.relative_to(bundle_dir))] = layer


def _hash_sources(paths: list[Path]) -> str:
    hasher = hashlib.sha256()
    for root in paths:
        if not root.exists():
            continue
        if root.is_file():
            _hash_file(hasher, root, root.name)
            continue
        for path in sorted(root.rglob("*")):
            if any(part in SKIP_DIRS for part in path.parts) or path.name in SKIP_FILES or path.is_dir():
                continue
            _hash_file(hasher, path, str(path.relative_to(root)))
    return "sha256:" + hasher.hexdigest()


def _hash_file(hasher: Any, path: Path, relative_name: str) -> None:
    hasher.update(relative_name.encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(path.read_bytes())
    hasher.update(b"\0")


def _hash_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _validate_no_plaintext_secrets(bundle_dir: Path) -> list[str]:
    warnings: list[str] = []
    for path in sorted(bundle_dir.rglob("*")):
        if path.is_dir() or any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in {".yaml", ".yml"} or path.name in {".mcp.json", "settings.json"}:
            warnings.extend(_scan_structured_secret_values(path, bundle_dir))
    return warnings


def _scan_structured_secret_values(path: Path, bundle_dir: Path) -> list[str]:
    try:
        if path.suffix.lower() == ".json" or path.name.endswith(".json"):
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, yaml.YAMLError):
        return []

    warnings: list[str] = []

    def visit(value: Any, location: str) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                key_text = str(key)
                nested_location = f"{location}.{key_text}" if location else key_text
                relative_path = path.relative_to(bundle_dir)
                is_plain_secret = isinstance(nested, str) and not _is_safe_secret_reference(nested)
                if _looks_secret_key(key_text) and is_plain_secret:
                    warnings.append(f"{relative_path} contains plaintext-looking secret at {nested_location}")
                if location.endswith("secrets") and is_plain_secret:
                    warnings.append(f"{relative_path} contains plaintext-looking secret at {nested_location}")
                visit(nested, nested_location)
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                visit(nested, f"{location}[{index}]")

    visit(data, "")
    return warnings


def _looks_secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(marker in normalized for marker in SECRET_KEY_MARKERS)


def _is_safe_secret_reference(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return True
    return (
        stripped.startswith("${")
        or stripped.startswith("ENC[")
        or stripped.startswith("<")
        or "replace" in stripped.lower()
    )


def git_commit_for_path(path: Path) -> str:
    git_binary = shutil.which("git")
    if not git_binary:
        return ""
    try:
        result = subprocess.run(  # noqa: S603 - read-only git metadata lookup for operator-provided repo path.
            [git_binary, "-C", str(path), "rev-parse", "HEAD"],
            check=True,
            text=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    return result.stdout.strip()
