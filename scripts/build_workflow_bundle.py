#!/usr/bin/env python3
"""Build an Agentic Ops workflow runtime bundle."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.lib.workflow_bundles import WorkflowRepoMetadata, build_workflow_bundle  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", help="Workflow name to bundle")
    parser.add_argument("--workflow-root", action="append", type=Path, required=True, help="Workflow root or repo root")
    parser.add_argument("--output-dir", type=Path, default=Path("dist/bundles"), help="Bundle output directory")
    parser.add_argument("--platform-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--repo-name", default="local")
    parser.add_argument("--repo-url", default="")
    parser.add_argument("--repo-ref", default="")
    parser.add_argument("--repo-commit", default="")
    args = parser.parse_args()

    result = build_workflow_bundle(
        workflow=args.workflow,
        output_dir=args.output_dir,
        platform_root=args.platform_root,
        workflow_roots=args.workflow_root,
        repo_metadata=WorkflowRepoMetadata(
            name=args.repo_name,
            url=args.repo_url,
            ref=args.repo_ref,
            commit=args.repo_commit,
        ),
        created_at=datetime.now(UTC),
    )
    print(f"Built {result.workflow} bundle at {result.bundle_dir}")
    print(f"Manifest: {result.manifest_path}")


if __name__ == "__main__":
    main()
