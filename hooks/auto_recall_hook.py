#!/usr/bin/env python3
"""SessionStart hook — auto-recall from Hindsight for the coordinator task.

Queries Hindsight for memories relevant to the user's prompt and injects
them as invisible additionalContext.  This gives agents baseline long-term
memory context without requiring explicit recall_similar calls in their prompts.

The Hindsight MCP resolves the correct memory bank from the
X-Task-Workflow header, so no workflow-specific config is needed here.

Hook response: on success, returns hookSpecificOutput with
additionalContext.  On failure or empty recall, exits silently so the
prompt proceeds unchanged.
"""

import json
import os
import sys
import tempfile

import httpx

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://hindsight:8888")
GATEWAY_EVENT_URL = os.environ.get("GATEWAY_EVENT_URL", "")
RECALL_TIMEOUT = 20.0  # seconds — leave time for bank lookup and event reporting within the hook's 30s budget
RECALL_LIMIT = 3
MAX_QUERY_CHARS = 800
HINDSIGHT_BANKS_PREFIX = "/v1/default/banks"


def emit_hook_event(*, status: str, detail: str = "", hook_event: str = "SessionStart"):
    task_id = os.environ.get("TASK_ID", "")
    if not GATEWAY_EVENT_URL or not task_id:
        print(f"[auto_recall_hook] skipping emit: URL={GATEWAY_EVENT_URL!r} TASK_ID={task_id!r}", file=sys.stderr)
        return

    try:
        resp = httpx.post(
            GATEWAY_EVENT_URL,
            json={
                "task_id": task_id,
                "event_type": "hook_event",
                "data": {
                    "hook_name": "auto_recall",
                    "hook_event": hook_event,
                    "status": status,
                    "detail": detail[:1000],
                },
            },
            timeout=2.5,
        )
        print(f"[auto_recall_hook] emitted {status} -> {resp.status_code}", file=sys.stderr)
    except Exception as exc:
        print(f"[auto_recall_hook] emit failed: {exc}", file=sys.stderr)


def _gateway_api_base() -> str:
    explicit = str(os.environ.get("GATEWAY_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    if GATEWAY_EVENT_URL.endswith("/events"):
        return GATEWAY_EVENT_URL[: -len("/events")]
    return "http://gateway:8080"


def _catalog_bank_id(task_workflow: str) -> str | None:
    workflow = task_workflow.strip().lower()
    if not workflow:
        return None
    try:
        response = httpx.get(f"{_gateway_api_base()}/api/platform/memories", timeout=2.5)
        payload = response.json()
    except Exception as exc:
        print(f"[auto_recall_hook] memory catalog lookup failed: {exc}", file=sys.stderr)
        return None

    for bank in payload.get("hindsight_banks") or []:
        if not isinstance(bank, dict):
            continue
        if bank.get("kind") != "business":
            continue
        workflows = [str(item).strip().lower() for item in bank.get("workflows") or []]
        if workflow in workflows and bank.get("bank_id"):
            return str(bank["bank_id"])
    return None


def resolve_bank_id(task_workflow: str) -> str:
    override = str(os.environ.get("HINDSIGHT_BUSINESS_BANK_ID") or os.environ.get("HINDSIGHT_BANK_ID") or "").strip()
    if override:
        return override
    return _catalog_bank_id(task_workflow) or "incident-rca"


def _sentinel_path(task_id: str) -> str:
    safe_task_id = "".join(ch for ch in task_id if ch.isalnum() or ch in ("-", "_")) or "unknown"
    return os.path.join(tempfile.gettempdir(), f"auto-recall-once-{safe_task_id}")


def _claim_initial_recall(task_id: str) -> bool:
    if not task_id:
        return True
    path = _sentinel_path(task_id)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        # Fail open rather than suppressing recall entirely if the sentinel
        # cannot be written for an environment-specific reason.
        return True


def _limit_recall_result(result):
    if isinstance(result, dict):
        for key in ("items", "results"):
            value = result.get(key)
            if isinstance(value, list):
                limited = dict(result)
                limited[key] = value[:RECALL_LIMIT]
                return limited
        return result
    if isinstance(result, list):
        return result[:RECALL_LIMIT]
    return result


def main():
    """Read a coordinator SessionStart event, query Hindsight, inject context."""
    try:
        event = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        emit_hook_event(status="skipped", detail="Invalid hook payload")
        return

    # Hook input sets agent_id only inside a subagent. Do not recall from
    # child sessions or SendMessage-driven continuations.
    if str(event.get("agent_id") or "").strip():
        return

    hook_event = str(event.get("hook_event_name") or "SessionStart")
    prompt = str(event.get("user_prompt") or event.get("prompt") or os.environ.get("TASK_PROMPT") or "").strip()
    if not prompt:
        emit_hook_event(status="skipped", detail="Empty task prompt", hook_event=hook_event)
        return

    query = prompt[:MAX_QUERY_CHARS]
    task_id = os.environ.get("TASK_ID", "")
    task_workflow = os.environ.get("TASK_WORKFLOW", "")

    if not _claim_initial_recall(task_id):
        emit_hook_event(
            status="skipped",
            detail="Recall already injected for this task",
            hook_event=hook_event,
        )
        return

    bank_id = resolve_bank_id(task_workflow)

    try:
        resp = httpx.post(
            f"{HINDSIGHT_URL}{HINDSIGHT_BANKS_PREFIX}/{bank_id}/memories/recall",
            json={"query": query, "max_tokens": 1500, "limit": RECALL_LIMIT},
            timeout=RECALL_TIMEOUT,
        )
        if resp.status_code != 200:
            emit_hook_event(
                status="error",
                detail=f"HTTP {resp.status_code} from Hindsight API",
                hook_event=hook_event,
            )
            return

        result = _limit_recall_result(resp.json())
        if isinstance(result, dict):
            recall_items = result.get("items") or result.get("results") or []
            recall_count = len(recall_items) if isinstance(recall_items, list) else 0
        elif isinstance(result, list):
            recall_count = len(result)
        else:
            recall_count = 0

        recall_text = json.dumps(result, ensure_ascii=False, indent=2)

        if not recall_text or recall_text.strip() in ("", "[]", "{}", "null"):
            emit_hook_event(
                status="skipped",
                detail="No similar incidents returned",
                hook_event=hook_event,
            )
            return

        emit_hook_event(
            status="success",
            detail=f"Injected {recall_count} similar past incidents from Hindsight",
            hook_event=hook_event,
        )

        output = {
            "hookSpecificOutput": {
                "hookEventName": hook_event,
                "additionalContext": ("[Long-term memory — similar past incidents]\n" + recall_text),
            }
        }
        print(json.dumps(output))

    except httpx.TimeoutException:
        emit_hook_event(
            status="error",
            detail="Hindsight auto-recall timed out",
            hook_event=hook_event,
        )
        print("Hindsight auto-recall timed out — skipping", file=sys.stderr)
    except Exception as exc:
        emit_hook_event(status="error", detail=str(exc), hook_event=hook_event)
        print(f"Hindsight auto-recall error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
