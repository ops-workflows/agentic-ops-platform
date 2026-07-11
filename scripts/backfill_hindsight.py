from __future__ import annotations

import argparse
import logging
from typing import Any

import httpx
import psycopg
from shared.hooks import retain_incident_hook as retain_hook

from shared.lib.config import settings

logger = logging.getLogger(__name__)


def _gateway_base_url() -> str:
    return "http://127.0.0.1:8080"


def _psycopg_dsn() -> str:
    return settings.sync_dsn.replace("postgresql+psycopg://", "postgresql://", 1)


def _fetch_task(task_id: str) -> dict[str, Any]:
    response = httpx.get(f"{_gateway_base_url()}/api/tasks/{task_id}", timeout=10.0)
    response.raise_for_status()
    return response.json()


def _fetch_session_detail(task_id: str) -> dict[str, Any] | None:
    response = httpx.get(f"{_gateway_base_url()}/api/sessions/{task_id}", timeout=10.0)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def _result_text(task: dict[str, Any]) -> str:
    result = task.get("result")
    if isinstance(result, dict):
        value = result.get("result")
        if isinstance(value, str):
            return value.strip()
    if isinstance(result, str):
        return result.strip()
    return ""


def _session_result_text(session_detail: dict[str, Any] | None) -> str:
    for event in reversed((session_detail or {}).get("events") or []):
        if event.get("event_type") != "session_complete":
            continue
        data = event.get("data") or {}
        result = data.get("result")
        if isinstance(result, str) and result.strip():
            return result.strip()
    return ""


def _iter_succeeded_task_ids(workflow: str) -> list[str]:
    query = "SELECT id::text FROM task_queue.tasks WHERE workflow = %s AND status = 'succeeded' ORDER BY created"
    with psycopg.connect(_psycopg_dsn()) as conn, conn.cursor() as cursor:
        cursor.execute(query, (workflow,))
        return [row[0] for row in cursor.fetchall()]


def _backfill_task(task_id: str, *, dry_run: bool) -> tuple[str, str]:
    task = _fetch_task(task_id)
    workflow = str(task.get("workflow") or "")
    session_detail = _fetch_session_detail(task_id)
    result_text = _result_text(task) or _session_result_text(session_detail)
    if not result_text:
        raise RuntimeError(f"Task {task_id} has no result text to retain")

    task_prompt = str(task.get("prompt") or "")
    case_id = retain_hook._extract_case_id(result_text, task_prompt)
    issue = retain_hook._issue_summary(task_prompt, case_id, result_text)
    trace = retain_hook._build_session_trace(session_detail, result_text)

    business_bank_id = retain_hook.resolve_bank_id(workflow, "business")
    learning_bank_id = retain_hook.resolve_bank_id(workflow, "learning")

    if dry_run:
        return business_bank_id, learning_bank_id

    retain_hook._retain_memory(
        bank_id=business_bank_id,
        content=retain_hook._business_content(issue, result_text),
        context=f"Incident RCA for {case_id}",
        document_id=task_id,
        metadata={
            "case_id": case_id,
            "task_id": task_id,
            "type": "rca_analysis",
            "workflow": workflow,
            "bank_kind": "business",
            "issue": issue,
        },
    )
    retain_hook._retain_memory(
        bank_id=learning_bank_id,
        content=retain_hook._learning_content(issue, trace, result_text),
        context=f"Workflow learning trace for {case_id}",
        document_id=f"{task_id}:learning",
        metadata={
            "case_id": case_id,
            "task_id": task_id,
            "type": "workflow_learning_trace",
            "workflow": workflow,
            "bank_kind": "learning",
            "issue": issue,
            "result": retain_hook._result_summary(result_text),
            "repeated_tools": "; ".join(trace.get("repeated_tools") or []),
            "successful_tools": "; ".join(trace.get("successful_tools") or []),
            "tool_error_count": len(trace.get("tool_errors") or []),
        },
    )
    return business_bank_id, learning_bank_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill Hindsight banks from succeeded investigator tasks.")
    parser.add_argument("--workflow", required=True, help="Workflow name to backfill, e.g. incident-investigator")
    parser.add_argument("--task-id", action="append", default=[], help="Specific task id(s) to backfill")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be retained without writing")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    task_ids = args.task_id or _iter_succeeded_task_ids(args.workflow)
    if not task_ids:
        logger.info("No succeeded tasks found for workflow=%s", args.workflow)
        return 0

    logger.info("Processing %d task(s) for workflow=%s", len(task_ids), args.workflow)
    failures = 0
    for task_id in task_ids:
        try:
            business_bank_id, learning_bank_id = _backfill_task(task_id, dry_run=args.dry_run)
        except Exception as exc:
            failures += 1
            logger.warning("Skipping task=%s: %s", task_id, exc)
            continue
        logger.info(
            "%s task=%s business_bank=%s learning_bank=%s",
            "Would backfill" if args.dry_run else "Backfilled",
            task_id,
            business_bank_id,
            learning_bank_id,
        )

    if failures:
        logger.warning("Skipped %d task(s) during backfill", failures)
    return 0 if failures < len(task_ids) else 1


if __name__ == "__main__":
    raise SystemExit(main())
