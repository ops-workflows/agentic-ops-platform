"""
Session Entrypoint — runs inside ephemeral agent Docker containers.

This script is the CMD of the ai-ops-agent-runtime image. It:
1. Reads the task prompt + metadata from env vars
2. Loads Claude project settings from .claude/settings.json and wires can_use_tool
3. Invokes the Claude Agent SDK (which spawns the `claude` CLI binary as a
    subprocess with --output-format stream-json for bidirectional control)
4. Surfaces approvals and AskUserQuestion through the configured message bus
5. Captures EVERY SDK message (assistant, tool_use, tool_result, system) and
    posts them to the Gateway event collector for full conversation storage
6. Runs a heartbeat loop concurrently to keep the task alive
7. Reports session_complete with token/cost/turn aggregates

The Claude Code CLI binary (@anthropic-ai/claude-code, installed via npm) is
pre-baked into the container image. The Python SDK communicates with it over
stdin/stdout using a JSON streaming control protocol.

The plugin directory is expected to use the flat layout convention
(agents/, skills/, hooks/hooks.json, settings.json, .mcp.json).
Shared content (CLAUDE.md, hook executables, skills) is injected from
the shared directory, and the .claude/ project structure is assembled
from the flat layout during workspace preparation.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
import yaml

# Claude Agent SDK — spawns `claude` CLI as subprocess
from claude_agent_sdk import (
    ClaudeAgentOptions,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
    query,
)

from shared.lib.message_bus import MessageBus, build_message_bus

logging.basicConfig(level=logging.INFO, format="[entrypoint] %(message)s")
logger = logging.getLogger(__name__)

# ─── Configuration ───────────────────────────────────────────────────────────

TASK_ID = os.environ["TASK_ID"]
TASK_PROMPT = os.environ["TASK_PROMPT"]
TASK_METADATA = json.loads(os.environ.get("TASK_METADATA", "{}"))

# Source directories (read-only mounts from host)
PLUGIN_SOURCE_DIR = Path(os.environ.get("PLUGIN_SOURCE_DIR", "/plugin-src"))
WORKFLOW_BUNDLE_PATH = Path(os.environ.get("WORKFLOW_BUNDLE_PATH", "/workflow-bundle"))
WORKFLOW_BUNDLE_URI = os.environ.get("WORKFLOW_BUNDLE_URI", "").strip()
WORKFLOW_BUNDLE_CHECKSUM = os.environ.get("WORKFLOW_BUNDLE_CHECKSUM", "").strip()
MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", "/memory"))
MEMORY_COMPLETE_MARKER = MEMORY_DIR / ".agentic-ops-memory-complete"

# Workspace — writable staging area where Claude actually runs.
# Populated at startup from the read-only plugin source + shared content.
WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))

# PLUGIN_DIR is the effective project root. After staging, it points to
# WORKSPACE_DIR so the rest of the code (settings, hooks, SDK invocation)
# operates on the staged copy, never the host checkout.
PLUGIN_DIR = WORKSPACE_DIR
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://gateway:8080")
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "30"))
CONVERSATION_BATCH_SIZE = int(os.environ.get("CONVERSATION_BATCH_SIZE", "5") or "5")
CONVERSATION_BATCH_MAX_AGE_SEC = float(os.environ.get("CONVERSATION_BATCH_MAX_AGE_SEC", "30") or "30")
CONTROL_REQUEST_TIMEOUT_SEC = float(os.environ.get("CLAUDE_CONTROL_TIMEOUT_SEC", "180"))
QUERY_PROGRESS_TIMEOUT_SEC = float(os.environ.get("CLAUDE_QUERY_PROGRESS_TIMEOUT_SEC", "240"))
OPERATOR_APPROVAL_TIMEOUT_SEC = float(os.environ.get("OPERATOR_APPROVAL_TIMEOUT_SEC", "3600"))

# The Agent SDK's control-protocol `initialize` handshake can be slow when the
# `claude` CLI cold-starts (esp. while MCP servers boot). The SDK derives its
# initialize timeout from this env var (ms, floored at 60s), so we raise it to
# match CONTROL_REQUEST_TIMEOUT_SEC instead of monkeypatching the SDK.
os.environ.setdefault("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", str(int(CONTROL_REQUEST_TIMEOUT_SEC * 1000)))
MESSAGE_CHANNEL = os.environ.get("MESSAGE_CHANNEL", "")
MESSAGE_THREAD_ID = os.environ.get("MESSAGE_THREAD_ID", "")
ACTIVE_THREAD_ID = MESSAGE_THREAD_ID
MESSAGE_BUS_API_URL = os.environ.get("MESSAGE_BUS_API_URL", "")
MESSAGE_BUS_PROVIDER = os.environ.get("MESSAGE_BUS_PROVIDER", "mattermost").strip().lower() or "mattermost"
MAX_TURNS = int(os.environ.get("MAX_TURNS", "50"))
RUNTIME_TIMEOUT_SEC = int(os.environ.get("RUNTIME_TIMEOUT_SEC", "0") or "0")
MESSAGE_CHANNEL_ID = str(TASK_METADATA.get("channel_id") or os.environ.get("MESSAGE_CHANNEL_ID", ""))
MESSAGE_TEAM_ID = str(TASK_METADATA.get("team_id") or os.environ.get("MESSAGE_TEAM_ID", ""))
MESSAGE_TEAM_NAME = str(
    TASK_METADATA.get("team_domain") or TASK_METADATA.get("team_name") or os.environ.get("MESSAGE_TEAM_NAME", "")
)
CONTROL_PLANE_UI_URL = os.environ.get("CONTROL_PLANE_UI_URL", "").strip().rstrip("/")
CLAUDE_SETTINGS_PATH = PLUGIN_DIR / ".claude" / "settings.json"
HOOKS_CONFIG_PATH = PLUGIN_DIR / "hooks" / "hooks.json"
ASK_USER_QUESTION_REMINDER_ENABLED = os.environ.get(
    "ASK_USER_QUESTION_REMINDER_ENABLED", "true"
).strip().lower() not in {"0", "false", "no", "off"}
ASK_USER_QUESTION_REMINDER_MIN_TURNS = max(
    1,
    int(os.environ.get("ASK_USER_QUESTION_REMINDER_MIN_TURNS", "10") or "10"),
)
ASK_USER_QUESTION_REMINDER_TURN_RATIO = min(
    1.0,
    max(0.0, float(os.environ.get("ASK_USER_QUESTION_REMINDER_TURN_RATIO", "0.7") or "0.7")),
)
ASK_USER_QUESTION_REMINDER_TIME_RATIO = min(
    1.0,
    max(0.0, float(os.environ.get("ASK_USER_QUESTION_REMINDER_TIME_RATIO", "0.75") or "0.75")),
)
ASK_USER_QUESTION_REMINDER_RECENT_QUESTION_TURN_WINDOW = max(
    0,
    int(os.environ.get("ASK_USER_QUESTION_REMINDER_RECENT_QUESTION_TURN_WINDOW", "8") or "8"),
)
# Upper bound (seconds) the PreToolUse hook may block while collecting a human
# answer for AskUserQuestion. Must exceed the message-bus reply wait so the SDK
# does not cancel the hook mid-wait.
ASK_USER_QUESTION_HOOK_TIMEOUT_SEC = max(
    60,
    int(os.environ.get("ASK_USER_QUESTION_HOOK_TIMEOUT_SEC", "3600") or "3600"),
)

# Secrets injected by the session manager stay in the Claude process
# environment so project-scoped .mcp.json headers can use ${VAR} expansion.
# Bash is contained by Claude Code's project sandbox settings in
# .claude/settings.json.
_SECRET_ENV_KEYS: set[str] = set()

# ─── Shared Content Injection ────────────────────────────────────────────────

SHARED_DIR = Path(os.environ.get("SHARED_DIR", "/shared"))

# platform-config.yaml may be mounted separately or under SHARED_DIR. The harness reads only the
# secret NAMES from it (never decrypts) to build the sandbox credential
# deny-list so every platform-level secret is hidden from sandboxed Bash.
_platform_config_file = os.environ.get("PLATFORM_CONFIG_FILE", "").strip()
PLATFORM_CONFIG_PATH = Path(_platform_config_file) if _platform_config_file else SHARED_DIR / "platform-config.yaml"

# Platform-managed secret env vars injected by the session manager that are not
# declared in any plugin's agent.yaml secrets block, but must still be hidden
# from sandboxed Bash commands.
_HARNESS_SECRET_ENV_KEYS: frozenset[str] = frozenset({"MESSAGE_BUS_BOT_TOKEN"})

# Placeholder prefix used by the agent.yaml template's documentation stub. Such
# names are dropped when assembling the live sandbox credential deny-list.
_CREDENTIAL_PLACEHOLDER_PREFIX = "EXAMPLE_"


def _has_authored_claude_content(path: Path) -> bool:
    """Return True when a Claude project directory already has authored content."""
    return path.exists() and any(path.iterdir())


def _copy_missing_directory_contents(src_dir: Path, dest_dir: Path) -> None:
    """Copy files and directories from src_dir into dest_dir without overwriting."""
    if not src_dir.exists():
        return

    dest_dir.mkdir(parents=True, exist_ok=True)
    for child in src_dir.iterdir():
        dest = dest_dir / child.name
        if dest.exists():
            continue
        if child.is_dir():
            shutil.copytree(child, dest)
        elif child.is_file():
            shutil.copy2(child, dest)


def _safe_extract_tar(tar_path: Path, dest_dir: Path) -> None:
    """Extract a tarball, refusing any member that would escape dest_dir.

    Bundles are built by the trusted platform (shared/lib/workflow_bundles.py),
    but this still guards against a corrupted or tampered archive attempting a
    path-traversal (``../``) write outside the intended staging directory.
    """
    resolved_dest = dest_dir.resolve()
    with tarfile.open(tar_path, mode="r:gz") as tar:
        for member in tar.getmembers():
            member_path = (resolved_dest / member.name).resolve()
            if member_path != resolved_dest and resolved_dest not in member_path.parents:
                raise RuntimeError(f"Refusing to extract workflow bundle member outside destination: {member.name}")
        tar.extractall(resolved_dest)  # noqa: S202 - member paths validated above


def _download_and_extract_bundle(uri: str) -> Path:
    """Download an https workflow-bundle tarball and extract it to a staging dir."""
    staging_dir = Path(tempfile.mkdtemp(prefix="workflow-bundle-"))
    tar_path = staging_dir / "bundle.tar.gz"
    with httpx.stream("GET", uri, timeout=60.0, follow_redirects=True) as response:
        response.raise_for_status()
        with tar_path.open("wb") as handle:
            for chunk in response.iter_bytes():
                handle.write(chunk)

    extract_dir = staging_dir / "extracted"
    extract_dir.mkdir()
    _safe_extract_tar(tar_path, extract_dir)
    tar_path.unlink(missing_ok=True)
    return extract_dir


def _workflow_bundle_source() -> Path | None:
    if WORKFLOW_BUNDLE_PATH.exists():
        return WORKFLOW_BUNDLE_PATH
    if not WORKFLOW_BUNDLE_URI:
        return None
    parsed = urlparse(WORKFLOW_BUNDLE_URI)
    if parsed.scheme in {"", "file"}:
        path = Path(unquote(parsed.path if parsed.scheme else WORKFLOW_BUNDLE_URI))
        return path if path.exists() else None
    if parsed.scheme in {"http", "https"}:
        return _download_and_extract_bundle(WORKFLOW_BUNDLE_URI)
    raise RuntimeError(f"Unsupported WORKFLOW_BUNDLE_URI scheme: {parsed.scheme}")


def _verify_workflow_bundle_checksum(bundle_dir: Path) -> None:
    if not WORKFLOW_BUNDLE_CHECKSUM:
        return
    manifest = bundle_dir / "manifest.yaml"
    if not manifest.exists():
        raise RuntimeError("WORKFLOW_BUNDLE_CHECKSUM was provided but manifest.yaml is missing")
    actual = "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
    if actual != WORKFLOW_BUNDLE_CHECKSUM:
        raise RuntimeError(f"Workflow bundle checksum mismatch: expected {WORKFLOW_BUNDLE_CHECKSUM}, got {actual}")


_BWRAP_SUPPORT_PROBE: bool | None = None


def _bubblewrap_supported() -> bool:
    """Return True when the current container can create bubblewrap namespaces."""
    global _BWRAP_SUPPORT_PROBE
    if _BWRAP_SUPPORT_PROBE is not None:
        return _BWRAP_SUPPORT_PROBE

    bwrap_path = shutil.which("bwrap")
    if not bwrap_path:
        _BWRAP_SUPPORT_PROBE = False
        return False

    try:
        probe = subprocess.run(  # noqa: S603 - fixed argv probes local bubblewrap support only.
            [bwrap_path, "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "/bin/true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        _BWRAP_SUPPORT_PROBE = probe.returncode == 0
    except Exception:
        _BWRAP_SUPPORT_PROBE = False

    return _BWRAP_SUPPORT_PROBE


def _prepare_workspace() -> None:
    """Stage a writable workspace from read-only plugin source + shared content.

    The host plugin directory is mounted read-only at PLUGIN_SOURCE_DIR.
    Plugins use a flat layout:

        agents/           — agent definitions (*.md with YAML frontmatter)
        skills/           — plugin-specific domain skills (*/SKILL.md)
        hooks/hooks.json  — hook event → command registrations
        settings.json     — Claude project settings (permissions, sandbox, agent)
        .mcp.json         — MCP server definitions
        agent.yaml        — platform config (secrets, schedules, messaging)

    This function copies those into WORKSPACE_DIR, assembles the .claude/
    project structure that Claude Code expects, injects shared content,
    merges hooks into .claude/settings.json, applies runtime sandbox
    overrides, and symlinks the persistent memory volume.

    After this returns, PLUGIN_DIR (== WORKSPACE_DIR) is the complete,
    writable Claude project root.
    """
    bundle_source = _workflow_bundle_source()
    source_dir = bundle_source or PLUGIN_SOURCE_DIR
    if bundle_source:
        _verify_workflow_bundle_checksum(bundle_source)

    logger.info("Staging workspace from %s -> %s", source_dir, WORKSPACE_DIR)
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Copy workflow source into workspace (flat files)
    if source_dir.exists():
        for child in source_dir.iterdir():
            dest = WORKSPACE_DIR / child.name
            if dest.exists():
                continue
            if child.is_dir():
                shutil.copytree(child, dest)
            elif child.is_file():
                shutil.copy2(child, dest)
        logger.info("Copied workflow source to workspace")

    # 2. Create .claude/ project structure from flat plugin layout
    claude_dir = WORKSPACE_DIR / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    # settings.json → .claude/settings.json
    root_settings = WORKSPACE_DIR / "settings.json"
    claude_settings = claude_dir / "settings.json"
    if root_settings.exists() and not claude_settings.exists():
        shutil.copy2(root_settings, claude_settings)
        logger.info("Placed settings.json -> .claude/settings.json")

    # agents/ → .claude/agents/
    project_agents_dir = claude_dir / "agents"
    if not _has_authored_claude_content(project_agents_dir):
        _copy_missing_directory_contents(WORKSPACE_DIR / "agents", project_agents_dir)

    # skills/ → .claude/skills/
    project_skills_dir = claude_dir / "skills"
    if not _has_authored_claude_content(project_skills_dir):
        _copy_missing_directory_contents(WORKSPACE_DIR / "skills", project_skills_dir)

    # 3. Inject shared content
    if not bundle_source and SHARED_DIR.exists():
        # CLAUDE.md
        shared_claude = SHARED_DIR / "CLAUDE.md"
        workspace_claude = WORKSPACE_DIR / "CLAUDE.md"
        if shared_claude.exists():
            if workspace_claude.exists():
                shared_text = shared_claude.read_text(encoding="utf-8")
                workspace_text = workspace_claude.read_text(encoding="utf-8")
                if shared_text not in workspace_text:
                    workspace_claude.write_text(
                        shared_text.rstrip() + "\n\n" + workspace_text,
                        encoding="utf-8",
                    )
                    logger.info("Merged shared CLAUDE.md into plugin CLAUDE.md")
            else:
                shutil.copy2(shared_claude, workspace_claude)
                logger.info("Injected shared CLAUDE.md")

        # Shared hook executables (.py only — hooks.json is per-plugin)
        shared_hooks_dir = SHARED_DIR / "hooks"
        workspace_hooks_dir = WORKSPACE_DIR / "hooks"
        if shared_hooks_dir.exists():
            workspace_hooks_dir.mkdir(exist_ok=True)
            for hook_file in shared_hooks_dir.iterdir():
                if hook_file.is_file() and hook_file.suffix == ".py":
                    dest = workspace_hooks_dir / hook_file.name
                    if not dest.exists():
                        shutil.copy2(hook_file, dest)
                        os.chmod(dest, 0o755)  # noqa: S103 - hook executables must be runnable in the session.
            logger.info("Injected shared hook executables")

        # Shared skills (merged into .claude/skills/)
        shared_skills_dir = SHARED_DIR / "skills"
        if shared_skills_dir.exists():
            _copy_missing_directory_contents(shared_skills_dir, project_skills_dir)
            logger.info("Injected shared skills")

    # 4. Merge hooks/hooks.json entries into .claude/settings.json
    _merge_plugin_hooks_into_claude_settings()

    # 5. Apply runtime sandbox overrides (bubblewrap fallback)
    _apply_runtime_claude_settings_overrides()

    # 5b. Deny every known secret env var to sandboxed Bash so the LLM can
    #     never read a credential value through a shell command. The parent
    #     Claude process still expands ${VAR} in .mcp.json for MCP auth.
    _apply_secret_credential_denies()

    # 6. Symlink persistent memory volume into Claude's expected path
    memory_link = claude_dir / "agent-memory"
    if MEMORY_DIR.exists() and not memory_link.exists():
        os.symlink(str(MEMORY_DIR), str(memory_link))
        logger.info("Linked memory volume %s -> %s", MEMORY_DIR, memory_link)


# ─── Secret Loading ──────────────────────────────────────────────────────────


def _load_secret_env() -> dict[str, str]:
    """Collect injected secret env vars for harness use.

    Secrets remain in the process environment so Claude Code can expand ${VAR}
    placeholders in .mcp.json. The harness also keeps a copy for approvals and
    thread polling.
    """
    # Load agent.yaml to find secret key names
    agent_yaml_path = PLUGIN_DIR / "agent.yaml"
    secret_env: dict[str, str] = {}

    if agent_yaml_path.exists():
        try:
            config = yaml.safe_load(agent_yaml_path.read_text())
            for key in config.get("secrets", {}):
                _SECRET_ENV_KEYS.add(key)
        except Exception:
            logger.warning("Failed to parse agent.yaml for secret keys")

    # Copy secret values from os.environ → returned dict
    for key in _SECRET_ENV_KEYS:
        val = os.environ.get(key)
        if val is not None:
            secret_env[key] = val

    return secret_env


# ─── Heartbeat ───────────────────────────────────────────────────────────────

_http_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=30.0)
    return _http_client


async def heartbeat_loop():
    """Send periodic heartbeats to the Gateway."""
    client = await _get_client()
    while True:
        try:
            response = await client.post(
                f"{GATEWAY_URL}/events",
                json={"task_id": TASK_ID, "event_type": "heartbeat", "data": {"timestamp": time.time()}},
                timeout=10,
            )
            response.raise_for_status()
        except Exception as e:
            logger.warning("Heartbeat error: %s", e)
        await asyncio.sleep(HEARTBEAT_INTERVAL)


# ─── Event Reporting ─────────────────────────────────────────────────────────

MAX_INLINE_SIZE = 10 * 1024  # 10KB
TRUNCATION_MARKER = "\n...[truncated by runtime harness]"


def _truncate_event_text(value: str, limit: int = MAX_INLINE_SIZE) -> str:
    """Bound UTF-8 text stored in task and session event payloads."""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    marker = TRUNCATION_MARKER.encode("utf-8")
    clipped = encoded[: max(0, limit - len(marker))].decode("utf-8", errors="ignore")
    return clipped + TRUNCATION_MARKER


async def report_event(event_type: str, data: dict[str, Any]) -> None:
    """Report an event to the Gateway event collector."""
    client = await _get_client()
    try:
        response = await client.post(
            f"{GATEWAY_URL}/events",
            json={
                "task_id": TASK_ID,
                "event_type": event_type,
                "data": dict(data),
            },
        )
        response.raise_for_status()
    except Exception as e:
        logger.warning("Failed to report %s: %s", event_type, e)


def _build_terminal_event_payload(progress_state: dict[str, Any], *, error: str) -> dict[str, Any]:
    """Build a best-effort terminal snapshot for timeout/error events."""
    payload: dict[str, Any] = {
        "error": _truncate_event_text(error),
        "input_tokens": int(progress_state.get("input_tokens") or 0),
        "output_tokens": int(progress_state.get("output_tokens") or 0),
        "turns": int(progress_state.get("turns") or 0),
        "total_messages": int(progress_state.get("total_messages") or 0),
        "total_cost_usd": float(progress_state.get("total_cost_usd") or 0.0),
        "model_usage": progress_state.get("model_usage") or {},
    }

    large_parts: dict[str, str] = {}
    last_assistant_message = str(progress_state.get("last_assistant_message") or "").strip()
    if last_assistant_message:
        payload["last_assistant_message_preview"] = last_assistant_message[:2000]
        large_parts["last_assistant_message"] = _truncate_event_text(last_assistant_message)

    last_result_text = str(progress_state.get("last_result_text") or "").strip()
    if last_result_text:
        payload["last_result_text_preview"] = last_result_text[:2000]
        large_parts["last_result_text"] = _truncate_event_text(last_result_text)

    if large_parts:
        payload.update(large_parts)

    return payload


def _terminal_error_text(error: str, progress_state: dict[str, Any]) -> str:
    """Prefer the model response when the SDK emits an unhelpful result subtype."""
    if "Claude Code returned an error result" not in error:
        return error
    result_text = str(progress_state.get("last_result_text") or "").strip()
    return result_text or error


# ─── Approval Gate (can_use_tool) ────────────────────────────────────────────
# This is the SDK's permission and user-input integration point. Both tool
# approvals and AskUserQuestion arrive here. The harness surfaces the request
# into the originating message thread and blocks until a human replies.


def _load_json_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception:
        logger.warning("Failed to parse %s", path)
        return {}
    return data if isinstance(data, dict) else {}


def _load_claude_settings() -> dict[str, Any]:
    return _load_json_settings(CLAUDE_SETTINGS_PATH)


def _load_project_mcp_servers_config() -> dict[str, Any] | None:
    """Return the staged ``.mcp.json`` config for explicit SDK wiring."""
    config = _load_json_settings(PLUGIN_DIR / ".mcp.json")
    servers = config.get("mcpServers")
    if not isinstance(servers, dict) or not servers:
        return None
    return {"mcpServers": servers}


def _merge_plugin_hooks_into_claude_settings() -> None:
    """Bridge plugin hooks/hooks.json into Claude's project settings.

    Claude Code loads hook registrations from .claude/settings.json under the
    top-level "hooks" key. Our plugin package format stores them in
    hooks/hooks.json, so merge that file into the live Claude settings before
    the SDK starts.
    """
    if not HOOKS_CONFIG_PATH.exists():
        return

    try:
        hooks_config = json.loads(HOOKS_CONFIG_PATH.read_text())
    except Exception:
        logger.warning("Failed to parse %s", HOOKS_CONFIG_PATH)
        return

    hook_map = hooks_config.get("hooks", hooks_config)
    if not isinstance(hook_map, dict):
        logger.warning("Unexpected hook config shape in %s", HOOKS_CONFIG_PATH)
        return

    settings = _load_claude_settings()
    existing_hooks = settings.get("hooks")
    if not isinstance(existing_hooks, dict):
        existing_hooks = {}

    changed = False
    for event_name, matchers in hook_map.items():
        if not isinstance(matchers, list):
            logger.warning("Skipping non-list hook registration for %s", event_name)
            continue

        current_matchers = existing_hooks.get(event_name)
        merged_matchers: list[Any] = []
        seen_matchers: set[str] = set()

        for matcher in current_matchers if isinstance(current_matchers, list) else []:
            matcher_key = json.dumps(matcher, sort_keys=True)
            if matcher_key in seen_matchers:
                changed = True
                continue
            seen_matchers.add(matcher_key)
            merged_matchers.append(matcher)

        for matcher in matchers:
            matcher_key = json.dumps(matcher, sort_keys=True)
            if matcher_key in seen_matchers:
                continue
            seen_matchers.add(matcher_key)
            merged_matchers.append(matcher)

        if existing_hooks.get(event_name) != merged_matchers:
            existing_hooks[event_name] = merged_matchers
            changed = True

    if not changed:
        return

    settings["hooks"] = existing_hooks
    CLAUDE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")
    logger.info("Merged %s into %s", HOOKS_CONFIG_PATH, CLAUDE_SETTINGS_PATH)


def _apply_runtime_claude_settings_overrides() -> None:
    """Disable Claude's Linux sandbox when the container cannot use bubblewrap.

    The runtime already executes inside an ephemeral container with repo-scoped
    mounts and platform-level permission gating. Claude Code's Linux sandbox
    relies on user-namespace support that is not available in this deployment,
    so fail open to unsandboxed commands inside the container instead of hard-
    failing every Bash invocation with a bubblewrap namespace error.
    """
    if os.environ.get("CLAUDE_SANDBOX_FAIL_OPEN", "1").lower() not in {"1", "true", "yes", "on"}:
        return

    settings = _load_claude_settings()
    sandbox = settings.get("sandbox")
    if not isinstance(sandbox, dict) or not sandbox.get("enabled"):
        return

    if os.environ.get("CLAUDE_SANDBOX_ENABLE_WEAKER_NESTED", "").lower() in {"1", "true", "yes", "on"}:
        sandbox["enableWeakerNestedSandbox"] = True
        filesystem = sandbox.get("filesystem")
        if not isinstance(filesystem, dict):
            filesystem = {}
        allow_write = filesystem.get("allowWrite")
        if not isinstance(allow_write, list):
            allow_write = []
        if "/memory" not in allow_write:
            allow_write.append("/memory")
        filesystem["allowWrite"] = allow_write
        sandbox["filesystem"] = filesystem
        settings["sandbox"] = sandbox
        CLAUDE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        CLAUDE_SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")
        logger.info("Enabled Claude weak nested sandbox mode in %s", CLAUDE_SETTINGS_PATH)
        return

    if _bubblewrap_supported():
        return

    changed = False
    if sandbox.get("enabled") is not False:
        sandbox["enabled"] = False
        changed = True
    if sandbox.get("failIfUnavailable") is not False:
        sandbox["failIfUnavailable"] = False
        changed = True
    if sandbox.get("autoAllowBashIfSandboxed") is not False:
        sandbox["autoAllowBashIfSandboxed"] = False
        changed = True
    if sandbox.get("allowUnsandboxedCommands") is not True:
        sandbox["allowUnsandboxedCommands"] = True
        changed = True

    if not changed:
        return

    CLAUDE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")
    logger.info(
        "Disabled Claude sandbox in %s because bubblewrap namespaces are unavailable; "
        "using Docker isolation and unsandboxed command fallback",
        CLAUDE_SETTINGS_PATH,
    )


def _collect_secret_env_keys() -> set[str]:
    """Return all declared secret env var names from plugin and platform config."""
    names = {name for name in _SECRET_ENV_KEYS if name and not name.startswith(_CREDENTIAL_PLACEHOLDER_PREFIX)}

    for path in (PLUGIN_DIR / "agent.yaml", PLATFORM_CONFIG_PATH):
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except Exception:
            logger.warning("Failed to read secret names from %s", path)
            continue

        secrets = data.get("secrets", {})
        if not isinstance(secrets, dict):
            continue
        for name in secrets:
            if isinstance(name, str) and name and not name.startswith(_CREDENTIAL_PLACEHOLDER_PREFIX):
                names.add(name)

    return names


def _build_credential_envvars(
    *,
    discovered_names: set[str],
    existing_entries: list[Any],
) -> list[dict[str, str]]:
    """Merge discovered secret names with authored sandbox credential entries."""
    merged: dict[str, dict[str, str]] = {}

    for entry in existing_entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name or name.startswith(_CREDENTIAL_PLACEHOLDER_PREFIX):
            continue
        mode = entry.get("mode")
        merged[name] = {
            "name": name,
            "mode": mode if isinstance(mode, str) and mode else "deny",
        }

    for name in discovered_names:
        if not isinstance(name, str) or not name or name.startswith(_CREDENTIAL_PLACEHOLDER_PREFIX):
            continue
        merged.setdefault(name, {"name": name, "mode": "deny"})

    return [merged[name] for name in sorted(merged)]


def _resolve_claude_cli_path() -> str:
    """Return the image-pinned Claude CLI path used for runtime sessions."""
    configured_path = os.environ.get("CLAUDE_CLI_PATH", "").strip()
    if configured_path:
        return configured_path

    discovered_path = shutil.which("claude")
    if discovered_path:
        return discovered_path

    raise RuntimeError("Claude CLI not found on PATH; runtime requires the image-installed claude binary")


def _apply_secret_credential_denies() -> None:
    """Deny every known secret env var to sandboxed Bash commands.

    Claude Code's ``sandbox.credentials.envVars`` (mode ``deny``) unsets the
    named variables before each sandboxed command runs, while the parent Claude
    process keeps them for ``${VAR}`` expansion in ``.mcp.json`` MCP headers.
    The plugin's ``settings.json`` carries a dummy ``EXAMPLE_`` seed entry to
    document where the list lives; the effective list is generated dynamically
    from every declared secret (``agent.yaml`` + ``platform-config.yaml``) so a
    newly added platform- or plugin-level secret is protected automatically,
    with no per-plugin settings.json edit.

    NOTE: ``deny`` only takes effect while the sandbox is active. When the
    sandbox is disabled (e.g. bubblewrap unavailable on the host) this list is
    inert; Bash containment then relies on Docker isolation + permission rules.
    """
    settings = _load_claude_settings()
    sandbox = settings.get("sandbox")
    if not isinstance(sandbox, dict):
        # No sandbox configured for this plugin — credential denies are inert.
        return

    credentials = sandbox.get("credentials")
    if not isinstance(credentials, dict):
        credentials = {}

    current = credentials.get("envVars")
    envvars = _build_credential_envvars(
        discovered_names=_collect_secret_env_keys(),
        existing_entries=current if isinstance(current, list) else [],
    )
    if not envvars:
        return

    credentials["envVars"] = envvars
    sandbox["credentials"] = credentials
    settings["sandbox"] = sandbox

    CLAUDE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")
    logger.info(
        "Applied sandbox credential denies for %d secret var(s): %s",
        len(envvars),
        ", ".join(entry["name"] for entry in envvars),
    )


def _load_permission_config() -> tuple[list[str], list[str]]:
    """Load ask/deny tool patterns from Claude project settings."""
    settings = _load_claude_settings()
    permissions = settings.get("permissions", {})
    return permissions.get("ask", []), permissions.get("deny", [])


def _resolve_default_agent() -> str | None:
    """Return the primary agent named in ``.claude/settings.json``.

    The plugin's ``settings.json`` may declare ``"agent": "<name>"`` to select
    the coordinator agent that should drive the session's main loop. The Agent
    SDK forces ``--system-prompt ""`` and does not read this field on its own,
    so we surface it explicitly and pass it to the CLI via ``--agent`` (which
    "Overrides the 'agent' setting").
    """
    settings = _load_claude_settings()
    agent = settings.get("agent")
    if isinstance(agent, str) and agent.strip():
        return agent.strip()
    return None


def _tool_matches_pattern(tool_name: str, tool_input: dict, pattern: str) -> bool:
    """Check if a tool call matches an approval pattern.

    Patterns follow Claude Code syntax:
      "Bash(systemctl *)"  → matches Bash tool with command starting with systemctl
      "mcp__splunk__*"     → matches any Splunk MCP tool
      "Write(*)"           → matches any Write tool call
    """
    # Pattern: ToolName(glob)
    m = re.match(r"^(\w+)\((.+)\)$", pattern)
    if m:
        pat_tool, pat_arg = m.group(1), m.group(2)
        if tool_name != pat_tool:
            return False
        # For Bash, match against the command argument
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            # Convert glob to regex: "systemctl *" → "systemctl .*"
            regex = "^" + re.escape(pat_arg).replace(r"\*", ".*") + "$"
            return bool(re.match(regex, cmd))
        # For other tools, match against first string arg
        first_arg = next((v for v in tool_input.values() if isinstance(v, str)), "")
        regex = "^" + re.escape(pat_arg).replace(r"\*", ".*") + "$"
        return bool(re.match(regex, first_arg))

    # Pattern: mcp__server, mcp__server__tool, or mcp__server__*
    if pattern.startswith("mcp__"):
        if pattern.count("__") == 1:
            return tool_name == pattern or tool_name.startswith(f"{pattern}__")
        regex = "^" + re.escape(pattern).replace(r"\*", ".*") + "$"
        return bool(re.match(regex, tool_name))

    # Exact match
    return tool_name == pattern


def _get_message_bus_bot_token(secret_env: dict[str, str]) -> str:
    return secret_env.get("MESSAGE_BUS_BOT_TOKEN", "") or os.environ.get("MESSAGE_BUS_BOT_TOKEN", "")


def _set_active_thread_id(thread_id: str) -> None:
    global ACTIVE_THREAD_ID
    ACTIVE_THREAD_ID = thread_id
    if thread_id:
        logger.info("Using message-bus thread %s for task %s", thread_id, TASK_ID)


def _build_message_bus(secret_env: dict[str, str]) -> MessageBus:
    return build_message_bus(
        provider=MESSAGE_BUS_PROVIDER,
        client_factory=_get_client,
        api_url=MESSAGE_BUS_API_URL,
        bot_token=_get_message_bus_bot_token(secret_env),
        channel_id=MESSAGE_CHANNEL_ID,
        channel_name=MESSAGE_CHANNEL,
        team_id=MESSAGE_TEAM_ID,
        team_name=MESSAGE_TEAM_NAME,
        get_thread_id=_current_thread_id,
        set_thread_id=_set_active_thread_id,
    )


def _extract_tool_result_message(message: Any) -> dict[str, Any] | None:
    """Best-effort extraction for SDK UserMessage-wrapped ToolResultBlock payloads."""
    blocks = getattr(message, "content", None)
    if not isinstance(blocks, list) or not blocks:
        return None

    tool_result_block = None
    for block in blocks:
        if hasattr(block, "tool_use_id") and hasattr(block, "content"):
            tool_result_block = block
            break

    if tool_result_block is None:
        return None

    content_value = getattr(tool_result_block, "content", None)
    content_str = str(content_value) if content_value is not None else ""
    return {
        "type": "tool_result",
        "tool_use_id": getattr(tool_result_block, "tool_use_id", ""),
        "parent_tool_use_id": getattr(message, "parent_tool_use_id", None),
        "content_preview": content_str[:2000],
        "is_error": bool(getattr(tool_result_block, "is_error", False)),
    }


def _current_thread_id() -> str:
    return ACTIVE_THREAD_ID or MESSAGE_THREAD_ID


def _task_prompt_summary(limit: int = 220) -> str:
    """Return a compact single-line summary of the task prompt."""
    summary = re.sub(r"\s+", " ", TASK_PROMPT).strip()
    if len(summary) <= limit:
        return summary
    return summary[: limit - 3].rstrip() + "..."


def _session_details_url() -> str:
    """Return a session detail URL when the control-plane UI base URL is configured."""
    if not CONTROL_PLANE_UI_URL:
        return ""
    return f"{CONTROL_PLANE_UI_URL}/sessions/{TASK_ID}"


async def _post_thread_message(secret_env: dict[str, str], text: str) -> dict[str, Any] | None:
    """Post a message into the current task thread through the configured message bus."""
    posted = await _build_message_bus(secret_env).post_to_thread(text)
    return posted.raw if posted else None


async def _wait_for_thread_reply(
    secret_env: dict[str, str],
    *,
    started_after_ms: int,
    ignore_post_ids: set[str] | None = None,
    timeout_sec: int = 3600,
) -> dict[str, str] | None:
    """Poll the current task thread until a human replies through the configured message bus."""
    reply = await _build_message_bus(secret_env).wait_for_reply(
        started_after_ms=started_after_ms,
        ignore_message_ids=ignore_post_ids,
        timeout_sec=timeout_sec,
    )
    if not reply:
        return None
    return {"message": reply.message, "user_id": reply.user_id, "username": reply.username}


def _parse_question_response(response: str, question: dict[str, Any]) -> str:
    """Parse numeric or free-text responses for AskUserQuestion."""
    options = question.get("options", [])
    response = response.strip()

    try:
        parts = [part.strip() for part in response.split(",") if part.strip()]
        labels = []
        for part in parts:
            index = int(part) - 1
            if 0 <= index < len(options):
                labels.append(options[index]["label"])
        if labels:
            return ", ".join(labels) if question.get("multiSelect") else labels[0]
    except ValueError:
        pass

    return response


def _ask_user_question_reminder_text() -> str:
    return (
        "Harness reminder: if you are stuck, missing key facts, or need a human decision, "
        "summarize the problem and your findings so far, then use AskUserQuestion with one "
        "specific next-step question. If the investigation is progressing and you are close to "
        "a conclusion, continue without asking."
    )


def _subagent_no_output_retry_text() -> str:
    return (
        "The previous Agent subagent completed without returning any output. If that branch is still "
        "important, retry once with a narrower prompt and a clearly stated deliverable. If you already "
        "have enough verified evidence, continue the investigation yourself instead of retrying."
    )


def _transcript_text_message(
    role: str,
    text: str,
    *,
    source: str,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "timestamp": time.time(),
        "type": role,
        "content": [{"type": "text", "text": text[:4000]}],
        "meta": {"source": source},
    }
    if meta:
        payload["meta"].update(_json_safe_value(meta))
    return payload


def _is_subagent_no_output_result(tool_result_preview: str) -> bool:
    return "Subagent completed but returned no output." in tool_result_preview


def _estimated_turns(progress_state: dict[str, Any]) -> int:
    return int(progress_state.get("turns") or 0)


def _should_send_ask_user_question_reminder(
    progress_state: dict[str, Any],
    *,
    query_started_at: float,
) -> tuple[bool, dict[str, Any]]:
    if not ASK_USER_QUESTION_REMINDER_ENABLED:
        return False, {}
    if progress_state.get("ask_user_question_reminder_sent"):
        return False, {}
    if str(progress_state.get("last_result_text") or "").strip():
        return False, {}

    turns = _estimated_turns(progress_state)
    if turns < ASK_USER_QUESTION_REMINDER_MIN_TURNS:
        return False, {}

    last_question_turn = progress_state.get("last_ask_user_question_turn")
    if (
        isinstance(last_question_turn, int)
        and (turns - last_question_turn) <= ASK_USER_QUESTION_REMINDER_RECENT_QUESTION_TURN_WINDOW
    ):
        return False, {}

    elapsed_sec = max(0.0, time.monotonic() - query_started_at)
    turn_threshold = max(ASK_USER_QUESTION_REMINDER_MIN_TURNS, int(MAX_TURNS * ASK_USER_QUESTION_REMINDER_TURN_RATIO))
    time_threshold_sec = RUNTIME_TIMEOUT_SEC * ASK_USER_QUESTION_REMINDER_TIME_RATIO if RUNTIME_TIMEOUT_SEC > 0 else 0.0
    turn_triggered = MAX_TURNS > 0 and turns >= turn_threshold
    time_triggered = time_threshold_sec > 0 and elapsed_sec >= time_threshold_sec
    if not (turn_triggered or time_triggered):
        return False, {}

    return True, {
        "turns": turns,
        "elapsed_sec": round(elapsed_sec, 3),
        "turn_threshold": turn_threshold,
        "time_threshold_sec": round(time_threshold_sec, 3),
        "trigger": "turn_budget" if turn_triggered else "time_budget",
    }


async def _handle_ask_user_question(
    tool_input: dict[str, Any],
    secret_env: dict[str, str],
    progress_state: dict[str, Any],
    append_transcript_messages,
) -> PermissionResultAllow | PermissionResultDeny:
    """Route AskUserQuestion through the configured message bus."""
    await report_event(
        "permission_callback",
        {
            "tool_name": "AskUserQuestion",
            "kind": "ask_user_question",
            "thread_id_present": bool(_current_thread_id()),
        },
    )

    answers: dict[str, str] = {}
    progress_state["last_ask_user_question_turn"] = _estimated_turns(progress_state)
    await report_event(
        "user_question_requested",
        {
            "question_count": len(tool_input.get("questions", [])),
            "thread_id_present": bool(_current_thread_id()),
        },
    )
    for question in tool_input.get("questions", []):
        lines = [
            ":question: **Clarification Needed**",
            "",
            f"**{question.get('header', 'Question')}**",
            question.get("question", ""),
            "",
        ]

        for index, option in enumerate(question.get("options", []), start=1):
            description = option.get("description", "")
            if description:
                lines.append(f"{index}. **{option['label']}** — {description}")
            else:
                lines.append(f"{index}. **{option['label']}**")

        lines.append("")
        if question.get("multiSelect"):
            lines.append("Reply with option numbers separated by commas, or type your own answer.")
        else:
            lines.append("Reply with an option number, or type your own answer.")

        question_prompt = "\n".join(lines)
        await append_transcript_messages(
            [
                _transcript_text_message(
                    "assistant",
                    question_prompt,
                    source="ask_user_question_prompt",
                    meta={"question": question.get("question", "")},
                )
            ]
        )

        started_at_ms = int(time.time() * 1000)
        posted = await _post_thread_message(secret_env, question_prompt)
        if not posted:
            return PermissionResultDeny(
                message="Unable to deliver AskUserQuestion through the configured message bus",
                interrupt=True,
            )

        reply = await _wait_for_thread_reply(
            secret_env,
            started_after_ms=started_at_ms,
            ignore_post_ids={str(posted.get("id") or "")},
        )
        if reply is None:
            return PermissionResultDeny(
                message="Timed out waiting for user input through the configured message bus",
                interrupt=True,
            )

        await append_transcript_messages(
            [
                _transcript_text_message(
                    "user",
                    reply["message"],
                    source="ask_user_question_response",
                    meta={"question": question.get("question", "")},
                )
            ]
        )

        answers[question.get("question", "")] = _parse_question_response(reply["message"], question)

    await report_event("user_question_resolved", {"question_count": len(answers)})
    await _wait_for_resume_admission(reason="user_input")
    return PermissionResultAllow(
        updated_input={
            "questions": tool_input.get("questions", []),
            "answers": answers,
        }
    )


async def _request_operator_approval(
    tool_name: str,
    tool_input: dict[str, Any],
    secret_env: dict[str, str],
) -> tuple[bool, dict[str, str]]:
    """Request approval via Gateway and poll the approval status until resolved."""
    del secret_env

    request_id = uuid.uuid4().hex
    await report_event(
        "approval_requested",
        {
            "tool_name": tool_name,
            "tool_input_preview": json.dumps(tool_input, default=str)[:1000],
            "request_id": request_id,
            "task_prompt_summary": _task_prompt_summary(),
        },
    )

    client = await _get_client()
    deadline = time.time() + OPERATOR_APPROVAL_TIMEOUT_SEC
    params = {
        "task_id": TASK_ID,
        "tool_name": tool_name,
        "request_id": request_id,
    }
    while time.time() < deadline:
        try:
            response = await client.get(
                f"{GATEWAY_URL}/api/runtime/approvals/status",
                params=params,
                timeout=30,
            )
            if response.status_code == 404:
                await asyncio.sleep(1)
                continue
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.warning("Failed to poll approval status for %s: %s", tool_name, exc)
            await asyncio.sleep(2)
            continue

        status = str(payload.get("status") or "").strip().lower()
        if status == "pending":
            await asyncio.sleep(2)
            continue

        actor: dict[str, str] = {}
        if payload.get("resolved_by"):
            actor["approved_by"] = str(payload["resolved_by"])
        if payload.get("resolved_by_user_id"):
            actor["approved_by_user_id"] = str(payload["resolved_by_user_id"])
        if status == "approved":
            actor["approval_reply"] = "approve"
            await report_event("approval_wait_resolved", {"tool_name": tool_name, "approved": True})
            await _wait_for_resume_admission(reason="approval")
            return True, actor

        actor["approval_reply"] = "reject"
        if payload.get("reason"):
            actor["reason"] = str(payload["reason"])
        await report_event("approval_wait_resolved", {"tool_name": tool_name, "approved": False})
        await _wait_for_resume_admission(reason="approval")
        return False, actor

    logger.warning("Timed out waiting for approval for %s", tool_name)
    return False, {}


def build_pre_tool_use_hook(
    secret_env: dict[str, str],
    progress_state: dict[str, Any],
    append_transcript_messages,
):
    """Build the PreToolUse hook.

    Two responsibilities:
    1. Keep the SDK input stream open so ``can_use_tool`` keeps firing.
    2. Answer the built-in ``AskUserQuestion`` tool. On Claude CLI >= 2.1.187
       ``AskUserQuestion`` became a host-mediated dialog: in a headless/no-TTY
       runtime the CLI auto-resolves it with empty answers *before* the
       ``can_use_tool`` permission callback is consulted. A PreToolUse hook,
       however, fires first and can satisfy the tool by returning
       ``permissionDecision: "allow"`` with an ``updatedInput`` that carries the
       human's answers collected through the configured message bus.
    """

    async def pre_tool_use_hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: Any,
    ) -> dict[str, Any]:
        tool_name = str((input_data or {}).get("tool_name") or "")
        if tool_name != "AskUserQuestion":
            return {"continue_": True}

        tool_input = (input_data or {}).get("tool_input") or {}
        result = await _handle_ask_user_question(
            tool_input,
            secret_env,
            progress_state,
            append_transcript_messages,
        )
        if isinstance(result, PermissionResultAllow):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "updatedInput": result.updated_input,
                }
            }
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": getattr(result, "message", "Unable to collect user input"),
            }
        }

    return pre_tool_use_hook


async def _report_precompact(
    input_data: dict[str, Any],
    tool_use_id: str | None,
    context: Any,
) -> dict[str, Any]:
    """Surface SDK compaction attempts into the session event stream."""
    await report_event(
        "hook_event",
        {
            "hook_name": "runtime",
            "hook_event": "PreCompact",
            "status": "observed",
            "detail": json.dumps(_json_safe_value(input_data or {}))[:2000],
        },
    )
    return {}


def build_can_use_tool(
    approval_patterns: list[str],
    secret_env: dict[str, str],
    progress_state: dict[str, Any],
    append_transcript_messages,
):
    """Build the can_use_tool callback with the loaded approval patterns."""

    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """SDK permission callback — called before every tool execution.

        Checks tool against approval patterns from .claude/settings.json.
        For matching tools, posts through the message bus and waits for human response.
        AskUserQuestion is answered earlier by the PreToolUse hook, not here.
        """
        # Check if this tool call matches any approval pattern
        needs_approval = any(_tool_matches_pattern(tool_name, tool_input, pat) for pat in approval_patterns)

        if not needs_approval:
            return PermissionResultAllow(updated_input=tool_input)

        logger.info("Tool %s requires approval — requesting via Gateway", tool_name)
        await report_event(
            "permission_callback",
            {
                "tool_name": tool_name,
                "kind": "operator_approval",
                "thread_id_present": bool(_current_thread_id()),
            },
        )

        approved, actor = await _request_operator_approval(tool_name, tool_input, secret_env)
        denial_error = (
            f"Approval not granted for {tool_name}. If you can continue without this tool, do so. "
            "Otherwise, synthesize the findings you already have for this session, explain what remains unverified, "
            "and finish without retrying the blocked action."
        )

        if approved:
            return PermissionResultAllow(updated_input=tool_input)

        progress_state["approval_denied"] = {
            "tool_name": tool_name,
            "tool_input": _json_safe_value(tool_input),
            "reason": actor.get("reason") or denial_error,
        }
        return PermissionResultDeny(message=denial_error, interrupt=False)

    return can_use_tool


async def _wait_for_resume_admission(*, reason: str, timeout_sec: int = 3600) -> None:
    """Block the live runtime until the scheduler re-admits the task."""
    client = await _get_client()
    deadline = time.monotonic() + timeout_sec
    terminal_statuses = {"failed", "lost", "timed_out", "succeeded"}

    while time.monotonic() < deadline:
        try:
            response = await client.get(f"{GATEWAY_URL}/api/tasks/{TASK_ID}", timeout=30)
            if response.status_code == 404:
                await asyncio.sleep(1)
                continue
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.warning("Failed to poll resume admission for %s: %s", reason, exc)
            await asyncio.sleep(2)
            continue

        status = str(payload.get("status") or "").strip().lower()
        if status == "running":
            return
        if status in terminal_statuses:
            raise RuntimeError(f"Task left wait-resume flow while waiting for scheduler admission: {status}")
        await asyncio.sleep(1)

    raise TimeoutError(f"Timed out waiting for scheduler admission after {reason} resolved")


def _json_safe_value(value: Any) -> Any:
    """Convert SDK payloads into JSON-safe telemetry while preserving structure."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    return str(value)[:500]


def _tool_input_preview(tool_name: str, tool_input: Any) -> str:
    """Build a compact, stable preview for telemetry and trace rendering."""
    if not isinstance(tool_input, dict):
        return json.dumps(tool_input, default=str)[:500]

    if tool_name == "Agent":
        agent_name = next(
            (
                str(tool_input.get(key)).strip()
                for key in ("subagent_type", "agent", "agent_name", "name")
                if isinstance(tool_input.get(key), str) and str(tool_input.get(key)).strip()
            ),
            "",
        )
        description = str(tool_input.get("description") or "").strip()
        prompt = str(tool_input.get("prompt") or "").strip()

        preview_payload: dict[str, Any] = {}
        if agent_name:
            preview_payload["subagent_type"] = agent_name
        if description:
            preview_payload["description"] = description
        if prompt:
            preview_payload["prompt_preview"] = prompt[:300]
        if not preview_payload:
            preview_payload = {key: _json_safe_value(value) for key, value in tool_input.items()}
        return json.dumps(preview_payload, default=str)[:500]

    return json.dumps(tool_input, default=str)[:500]


def _humanize_tool_name(tool_name: str) -> str:
    """Convert internal tool names into a user-facing label."""
    raw_name = tool_name.strip()
    if not raw_name:
        return "the requested tool"

    if raw_name.startswith("mcp__"):
        parts = raw_name.split("__")
        if len(parts) >= 3:
            server_name = parts[1].replace("_", " ").strip()
            tool_label = parts[2].replace("_", " ").strip()
            combined = f"{server_name} {tool_label}".strip()
            if combined:
                return combined

    return raw_name.replace("__", " ").replace("_", " ").strip()


def _summarize_denied_tool_input(tool_input: dict[str, Any]) -> str:
    """Return a short summary of the blocked tool input for user-facing fallback text."""
    if not isinstance(tool_input, dict) or not tool_input:
        return ""

    priority_keys = (
        "query",
        "prompt",
        "command",
        "description",
        "record_id",
        "object_type",
        "path",
        "file_path",
        "url",
        "endpoint",
        "workflow",
        "name",
        "id",
    )

    parts: list[str] = []
    seen_keys: set[str] = set()

    for key in priority_keys:
        if key not in tool_input:
            continue
        value = _json_safe_value(tool_input.get(key))
        if isinstance(value, (dict, list, tuple)):
            value_text = json.dumps(value, default=str)[:120]
        else:
            value_text = str(value or "").strip()[:120]
        if not value_text:
            continue
        parts.append(f"{key}={value_text}")
        seen_keys.add(key)
        if len(parts) >= 2:
            break

    if len(parts) < 2:
        for key, value in tool_input.items():
            if key in seen_keys or value is None:
                continue
            if not isinstance(value, (str, int, float, bool)):
                continue
            value_text = str(value).strip()[:120]
            if not value_text:
                continue
            parts.append(f"{key}={value_text}")
            if len(parts) >= 2:
                break

    return ", ".join(parts)


def _approval_denied_fallback_text(denial: dict[str, Any]) -> str:
    """Build a generic user-facing fallback result when approval denial ends the flow."""
    tool_name = str(denial.get("tool_name") or "").strip()
    tool_input = denial.get("tool_input") if isinstance(denial.get("tool_input"), dict) else {}

    tool_label = _humanize_tool_name(tool_name)
    input_summary = _summarize_denied_tool_input(tool_input)

    message = f"I could not complete the requested action because access to {tool_label} was not approved."
    if input_summary:
        message += f" Blocked request: {input_summary}."
    message += (
        " I can only answer from already available information, and I do not have enough verified evidence "
        "to complete this reliably right now."
    )
    return message


# ─── Claude Agent SDK Invocation ─────────────────────────────────────────────


async def run_agent_session(secret_env: dict[str, str], progress_state: dict[str, Any]) -> dict[str, Any]:
    """Run Claude Code via the Agent SDK.

    The plugin directory is staged by _prepare_workspace() before this
    function runs.  After staging, the workspace (.claude/ project) contains:
    - CLAUDE.md                   (injected from shared/)
    - .mcp.json                   (authored per-plugin)
    - .claude/settings.json       (from plugin settings.json + merged hooks)
    - .claude/agents/*.md         (from plugin agents/)
    - .claude/skills/*/SKILL.md   (from plugin skills/ + shared skills)
    - hooks/*.py                  (shared executables, referenced by hooks.json)
    - hooks/hooks.json            (authored per-plugin)
    - .claude/agent-memory/       (symlink to persistent memory volume)

    The SDK spawns the `claude` CLI binary as a subprocess with
    --output-format stream-json. Communication happens over a
    bidirectional JSON control protocol on stdin/stdout.
    """
    approval_patterns, deny_patterns = _load_permission_config()

    permission_mode = os.environ.get("CLAUDE_PERMISSION_MODE") or None
    claude_cli_path = _resolve_claude_cli_path()
    options = ClaudeAgentOptions(
        tools={"type": "preset", "preset": "claude_code"},
        max_turns=MAX_TURNS,
        cwd=str(PLUGIN_DIR),
        cli_path=claude_cli_path,
        permission_mode=permission_mode,
    )

    project_mcp_servers = _load_project_mcp_servers_config()
    if project_mcp_servers is not None:
        options.mcp_servers = project_mcp_servers

    # Launch with the coordinator agent declared in settings.json ("agent": ...).
    # The SDK has no dedicated field for this, so pass it through --agent, which
    # the CLI documents as overriding the 'agent' setting.
    default_agent = _resolve_default_agent()
    if default_agent:
        options.extra_args = {**options.extra_args, "agent": default_agent}

    if deny_patterns:
        options.disallowed_tools = deny_patterns

    total_input_tokens = 0
    total_output_tokens = 0
    final_text = ""
    num_turns = 0
    total_cost_usd = 0.0
    model_usage: dict = {}
    conversation: list[dict[str, Any]] = []  # Full conversation transcript
    flushed_messages = 0
    pending_batch_started_at: float | None = None
    flush_lock = asyncio.Lock()
    tool_names_by_use_id: dict[str, str] = {}
    empty_subagent_retries: set[str] = set()
    progress_state.update(
        {
            "input_tokens": 0,
            "output_tokens": 0,
            "turns": 0,
            "total_cost_usd": 0.0,
            "model_usage": {},
            "total_messages": 0,
            "last_assistant_message": "",
            "last_result_text": "",
            "last_ask_user_question_turn": None,
            "ask_user_question_reminder_sent": False,
        }
    )

    async def flush_conversation_batches(*, force: bool = False) -> None:
        nonlocal flushed_messages, pending_batch_started_at
        async with flush_lock:
            while len(conversation) - flushed_messages >= CONVERSATION_BATCH_SIZE:
                next_boundary = flushed_messages + CONVERSATION_BATCH_SIZE
                await report_event(
                    "conversation_batch",
                    {
                        "messages": conversation[flushed_messages:next_boundary],
                        "turn_index": next_boundary,
                    },
                )
                flushed_messages = next_boundary

            if force and flushed_messages < len(conversation):
                await report_event(
                    "conversation_batch",
                    {
                        "messages": conversation[flushed_messages:],
                        "turn_index": len(conversation),
                    },
                )
                flushed_messages = len(conversation)

            if flushed_messages < len(conversation):
                if pending_batch_started_at is None:
                    pending_batch_started_at = time.monotonic()
            else:
                pending_batch_started_at = None

    async def conversation_batch_flush_loop() -> None:
        while True:
            await asyncio.sleep(1.0)
            if pending_batch_started_at is None:
                continue
            if time.monotonic() - pending_batch_started_at < CONVERSATION_BATCH_MAX_AGE_SEC:
                continue
            await flush_conversation_batches(force=True)

    async def append_transcript_messages(messages: list[dict[str, Any]]) -> None:
        if not messages:
            return
        conversation.extend(messages)
        progress_state["total_messages"] = len(conversation)
        await flush_conversation_batches()

    # In Python, can_use_tool requires streaming mode plus a PreToolUse hook.
    # The same PreToolUse hook also answers the built-in AskUserQuestion tool,
    # which the headless CLI would otherwise auto-resolve before can_use_tool
    # is consulted. A long timeout lets it block on a human reply.
    options.hooks = {
        "PreToolUse": [
            HookMatcher(
                matcher=None,
                hooks=[build_pre_tool_use_hook(secret_env, progress_state, append_transcript_messages)],
                timeout=ASK_USER_QUESTION_HOOK_TIMEOUT_SEC,
            )
        ],
        "PreCompact": [HookMatcher(matcher=None, hooks=[_report_precompact])],
    }
    options.can_use_tool = build_can_use_tool(
        approval_patterns,
        secret_env,
        progress_state,
        append_transcript_messages,
    )

    # Allowed tools override
    if os.environ.get("ALLOWED_TOOLS"):
        options.allowed_tools = os.environ["ALLOWED_TOOLS"].split(",")

    prompt_queue: asyncio.Queue[dict[str, Any] | None]
    prompt_stream_closed = False
    # Newer Claude CLI (>=2.1) launches subagents asynchronously: the parent
    # emits its ResultMessage *before* the subagent runs. If we close stdin at
    # that first result, the control stream dies and every subagent tool call
    # fails with "Tool permission request failed: Error: Stream closed" because
    # can_use_tool responses travel back over the same stdin channel. Track
    # in-flight async subagents so we only close stdin once none remain.
    pending_async_subagents = 0
    observed_turns = 0
    query_started_at = time.monotonic()

    def _new_prompt_queue(initial_content: str) -> asyncio.Queue[dict[str, Any] | None]:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        queue.put_nowait(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": initial_content,
                },
            }
        )
        return queue

    async def prompt_stream(queue: asyncio.Queue[dict[str, Any] | None]):
        while True:
            item = await queue.get()
            if item is None:
                return
            yield item

    prompt_queue = _new_prompt_queue(TASK_PROMPT)

    await report_event(
        "session_phase",
        {
            "phase": "claude_query_start",
            "approval_patterns": approval_patterns,
            "deny_patterns": deny_patterns,
        },
    )

    output_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    async def _stream_query_messages(
        queue: asyncio.Queue[dict[str, Any] | None],
    ) -> None:
        try:
            query_kwargs: dict[str, Any] = {
                "prompt": prompt_stream(queue),
                "options": options,
            }
            async for message in query(**query_kwargs):
                await output_queue.put(("message", message))
        except Exception as exc:
            await output_queue.put(("error", exc))
        else:
            await output_queue.put(("done", None))
        finally:
            await queue.put(None)

    def _start_query_attempt(*, initial_content: str = TASK_PROMPT) -> asyncio.Task:
        nonlocal prompt_queue, query_started_at
        prompt_queue = _new_prompt_queue(initial_content)
        query_started_at = time.monotonic()
        return asyncio.create_task(_stream_query_messages(prompt_queue))

    query_task = _start_query_attempt()
    timed_flush_task = asyncio.create_task(conversation_batch_flush_loop())
    first_message_received = False

    try:
        while True:
            try:
                event_type, payload = await asyncio.wait_for(
                    output_queue.get(),
                    timeout=QUERY_PROGRESS_TIMEOUT_SEC,
                )
            except TimeoutError as exc:
                query_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await query_task
                phase = "before_first_message" if not first_message_received else "between_messages"
                timeout_error = TimeoutError(f"No Claude SDK messages for {QUERY_PROGRESS_TIMEOUT_SEC:.0f}s ({phase})")
                raise timeout_error from exc

            if event_type == "done":
                break
            if event_type == "error":
                raise payload

            message = payload
            if not first_message_received:
                first_message_received = True

            # ── Capture EVERY message for full telemetry ──────────────
            # The SDK yields: AssistantMessage, UserMessage, SystemMessage,
            # ToolUseBlock, ToolResultBlock, and a final ResultMessage.
            # We capture all of them → event collector → Postgres.
            msg_record: dict[str, Any] = {"timestamp": time.time()}

            if hasattr(message, "content") and hasattr(message, "model"):
                # AssistantMessage — Claude's response
                observed_turns += 1
                msg_record["type"] = "assistant"
                msg_record["model"] = getattr(message, "model", "")
                # Subagent messages carry the spawning Task/Agent tool id so the
                # UI can nest their activity under the correct subagent branch.
                msg_record["parent_tool_use_id"] = getattr(message, "parent_tool_use_id", None)
                blocks = []
                assistant_text_parts: list[str] = []
                for block in getattr(message, "content", []):
                    if hasattr(block, "text"):
                        block_text = block.text or ""
                        blocks.append({"type": "text", "text": block_text[:2000]})
                        if block_text:
                            assistant_text_parts.append(block_text)
                    elif hasattr(block, "name"):
                        tool_names_by_use_id[str(block.id)] = str(block.name)
                        blocks.append(
                            {
                                "type": "tool_use",
                                "name": block.name,
                                "id": block.id,
                                "input_preview": _tool_input_preview(block.name, getattr(block, "input", {})),
                            }
                        )
                    elif hasattr(block, "thinking"):
                        blocks.append({"type": "thinking", "preview": block.thinking[:500]})
                msg_record["content"] = blocks
                assistant_text = "\n".join(part for part in assistant_text_parts if part).strip()
                if assistant_text:
                    progress_state["last_assistant_message"] = assistant_text

            elif hasattr(message, "tool_use_id") and hasattr(message, "content"):
                # ToolResultBlock
                msg_record["type"] = "tool_result"
                msg_record["tool_use_id"] = message.tool_use_id
                content_str = str(message.content) if message.content else ""
                msg_record["content_preview"] = content_str[:2000]
                msg_record["is_error"] = getattr(message, "is_error", False)

            elif (wrapped_tool_result := _extract_tool_result_message(message)) is not None:
                msg_record.update(wrapped_tool_result)

            elif hasattr(message, "result") and hasattr(message, "usage"):
                # ResultMessage — final message with aggregates
                msg_record["type"] = "result"
                final_text = getattr(message, "result", "") or ""
                num_turns = getattr(message, "num_turns", 0)
                total_cost_usd = getattr(message, "total_cost_usd", 0.0) or 0.0
                usage = getattr(message, "usage", None)
                if usage and isinstance(usage, dict):
                    total_input_tokens = usage.get("input_tokens", 0)
                    total_output_tokens = usage.get("output_tokens", 0)
                    msg_record["usage"] = {
                        "input_tokens": int(total_input_tokens or 0),
                        "output_tokens": int(total_output_tokens or 0),
                        "total_tokens": int((total_input_tokens or 0) + (total_output_tokens or 0)),
                    }
                model_usage = getattr(message, "modelUsage", {}) or {}
                msg_record["result_preview"] = final_text[:2000]
                msg_record["num_turns"] = num_turns
                msg_record["total_cost_usd"] = total_cost_usd
                progress_state["last_result_text"] = final_text
                # Once Claude has produced a final result AND no async
                # subagents are still in flight, close the prompt stream so the
                # SDK can end stdin and let the CLI terminate cleanly instead of
                # hanging in heartbeat-only state. If a subagent is still
                # running, keep stdin open so its tool permission requests can
                # be answered over the control stream.
                if pending_async_subagents <= 0 and not prompt_stream_closed:
                    prompt_stream_closed = True
                    await prompt_queue.put(None)

            elif hasattr(message, "subtype") and hasattr(message, "data"):
                # SystemMessage
                msg_record["type"] = "system"
                msg_record["subtype"] = message.subtype
                safe_data = _json_safe_value(message.data or {})
                msg_record["data"] = safe_data
                # Track async subagent lifecycle so we don't close stdin while a
                # subagent still needs to answer tool permission requests.
                if message.subtype == "task_started":
                    pending_async_subagents += 1
                elif message.subtype == "task_updated":
                    patch = safe_data.get("patch") if isinstance(safe_data, dict) else None
                    patch_status = patch.get("status") if isinstance(patch, dict) else None
                    if patch_status in ("completed", "failed", "cancelled", "canceled"):
                        pending_async_subagents = max(0, pending_async_subagents - 1)

            else:
                # Unknown message type — log it
                msg_record["type"] = "unknown"
                msg_record["repr"] = repr(message)[:500]

            conversation.append(msg_record)
            progress_state["input_tokens"] = int(total_input_tokens or 0)
            progress_state["output_tokens"] = int(total_output_tokens or 0)
            progress_state["turns"] = max(int(num_turns or 0), observed_turns)
            progress_state["total_cost_usd"] = float(total_cost_usd or 0.0)
            progress_state["model_usage"] = model_usage or {}
            progress_state["total_messages"] = len(conversation)
            await flush_conversation_batches()

            if (
                msg_record.get("type") == "tool_result"
                and tool_names_by_use_id.get(str(msg_record.get("tool_use_id") or "")) == "Agent"
            ):
                tool_use_id = str(msg_record.get("tool_use_id") or "")
                preview = str(msg_record.get("content_preview") or "")
                if tool_use_id and tool_use_id not in empty_subagent_retries and _is_subagent_no_output_result(preview):
                    empty_subagent_retries.add(tool_use_id)
                    retry_text = _subagent_no_output_retry_text()
                    await prompt_queue.put(
                        {
                            "type": "user",
                            "message": {
                                "role": "user",
                                "content": retry_text,
                            },
                        }
                    )
                    await append_transcript_messages(
                        [
                            _transcript_text_message(
                                "user",
                                retry_text,
                                source="subagent_no_output_retry",
                                meta={"tool_use_id": tool_use_id},
                            )
                        ]
                    )
                    await report_event(
                        "subagent_no_output",
                        {
                            "tool_use_id": tool_use_id,
                            "retry_injected": True,
                        },
                    )

            should_remind, reminder_context = _should_send_ask_user_question_reminder(
                progress_state,
                query_started_at=query_started_at,
            )
            if should_remind:
                progress_state["ask_user_question_reminder_sent"] = True
                await prompt_queue.put(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": _ask_user_question_reminder_text(),
                        },
                    }
                )
                await append_transcript_messages(
                    [
                        _transcript_text_message(
                            "user",
                            _ask_user_question_reminder_text(),
                            source="ask_user_question_reminder",
                            meta=reminder_context,
                        )
                    ]
                )
                await report_event("ask_user_question_reminder", reminder_context)
    finally:
        if not query_task.done():
            query_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await query_task
        timed_flush_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await timed_flush_task

    # Flush remaining conversation messages
    await flush_conversation_batches(force=True)

    approval_denied = progress_state.get("approval_denied")
    if not final_text and isinstance(approval_denied, dict):
        final_text = _approval_denied_fallback_text(approval_denied)

    await report_event(
        "session_phase",
        {
            "phase": "claude_query_complete",
            "total_messages": len(conversation),
        },
    )

    return {
        "result": _truncate_event_text(final_text),
        "result_preview": final_text[:5000],
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "total_cost_usd": total_cost_usd,
        "model_usage": model_usage,
        "turns": max(num_turns, observed_turns),
        "total_messages": len(conversation),
    }


# ─── Main ────────────────────────────────────────────────────────────────────


async def main():
    logger.info("Starting session for task %s", TASK_ID)
    logger.info("Prompt: %s...", TASK_PROMPT[:200])
    logger.info("Plugin dir: %s", PLUGIN_DIR)

    if not PLUGIN_SOURCE_DIR.exists():
        logger.error("Plugin source dir %s not found", PLUGIN_SOURCE_DIR)
        _signal_kubernetes_memory_sync_complete()
        sys.exit(1)

    try:
        _prepare_workspace()
    except Exception:
        _signal_kubernetes_memory_sync_complete()
        raise

    secret_env = _load_secret_env()
    logger.info("Loaded %d secret env vars for harness + MCP auth", len(secret_env))

    await report_event(
        "session_start",
        {
            "prompt_preview": TASK_PROMPT[:2000],
            "agent": _resolve_default_agent(),
            "workflow": os.environ.get("TASK_WORKFLOW", ""),
            "channel": MESSAGE_CHANNEL,
            "thread_id": MESSAGE_THREAD_ID,
        },
    )

    heartbeat_task = asyncio.create_task(heartbeat_loop())

    progress_state: dict[str, Any] = {}

    try:
        result = await run_agent_session(secret_env, progress_state)
        await report_event("session_complete", result)
    except TimeoutError as exc:
        await report_event(
            "session_timeout",
            _build_terminal_event_payload(progress_state, error=str(exc) or "Session timed out"),
        )
        raise
    except Exception as e:
        logger.error("Error: %s", e)
        await report_event(
            "session_error",
            _build_terminal_event_payload(progress_state, error=_terminal_error_text(str(e), progress_state)),
        )
        raise
    finally:
        heartbeat_task.cancel()
        if _http_client:
            await _http_client.aclose()
        _signal_kubernetes_memory_sync_complete()

    logger.info("Session complete for task %s", TASK_ID)


def handle_signal(sig, frame):
    logger.info("Received signal %s, shutting down...", sig)
    _signal_kubernetes_memory_sync_complete()
    sys.exit(0)


def _signal_kubernetes_memory_sync_complete() -> None:
    if os.environ.get("KUBERNETES_MEMORY_SYNC") == "1":
        MEMORY_COMPLETE_MARKER.touch()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    asyncio.run(main())
