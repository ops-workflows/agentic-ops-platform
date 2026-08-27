"""Contract tests for deterministic Knowledge Source Graphify extraction."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import zstandard

from shared.lib.knowledge_source_graphify import (
    GRAPHIFY_VERSION,
    GraphifyOutputError,
    KnowledgeSourceBundleError,
    build_knowledge_source_bundle,
    run_graphify_code_extraction,
    validate_graphify_output,
    verify_knowledge_source_bundle,
)

pytestmark = pytest.mark.unit


def _write_graphify_output(output_dir: Path) -> None:
    output_dir.mkdir(parents=True)
    (output_dir / "graph.json").write_text(
        json.dumps(
            {
                "meta": {"project_name": "example"},
                "nodes": [{"id": "module:app", "name": "app", "source_file": "app.py"}],
                "links": [],
                "hyperedges": [],
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "manifest.json").write_text(
        json.dumps({"app.py": {"mtime": 1.0, "ast_hash": "abc", "semantic_hash": ""}}),
        encoding="utf-8",
    )
    (output_dir / "GRAPH_REPORT.md").write_text("# Graph report\n", encoding="utf-8")


def test_run_graphify_uses_one_pinned_code_only_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    output_root = tmp_path / "output"
    output_dir = output_root / "graphify-out"
    observed: dict[str, object] = {"commands": []}

    def _fake_run(command, *, check, capture_output, text, timeout):
        observed["commands"].append(command)
        observed.update(
            check=check,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
        )
        if command[1] == "extract":
            _write_graphify_output(output_dir)
            (output_dir / "GRAPH_REPORT.md").unlink()
            return SimpleNamespace(stdout="Graph: 1 nodes, 0 edges", stderr="")
        (output_dir / "GRAPH_REPORT.md").write_text("# Graph report\n", encoding="utf-8")
        return SimpleNamespace(stdout="Done - report updated", stderr="")

    monkeypatch.setattr("shared.lib.knowledge_source_graphify.subprocess.run", _fake_run)

    result = run_graphify_code_extraction(
        source_dir,
        output_root,
        graphify_binary="/opt/graphify/bin/graphify",
        timeout_sec=90,
    )

    assert GRAPHIFY_VERSION == "0.9.40"
    assert observed["commands"] == [
        [
            "/opt/graphify/bin/graphify",
            "extract",
            str(source_dir.resolve()),
            "--code-only",
            "--max-workers",
            "1",
            "--no-cluster",
            "--out",
            str(output_root.resolve()),
        ],
        [
            "/opt/graphify/bin/graphify",
            "cluster-only",
            str(output_root.resolve()),
            "--no-viz",
        ],
    ]
    assert observed["timeout"] == 90
    assert result.output_dir == output_dir.resolve()
    assert result.node_count == 1
    assert result.edge_count == 0
    assert result.file_count == 1
    assert result.stdout == "Graph: 1 nodes, 0 edges\nDone - report updated"


def test_validate_graphify_output_rejects_missing_required_artifact(tmp_path: Path):
    output_dir = tmp_path / "graphify-out"
    _write_graphify_output(output_dir)
    (output_dir / "manifest.json").unlink()

    with pytest.raises(GraphifyOutputError, match="manifest.json"):
        validate_graphify_output(output_dir)


def test_validate_graphify_output_rejects_unknown_graph_shape(tmp_path: Path):
    output_dir = tmp_path / "graphify-out"
    _write_graphify_output(output_dir)
    (output_dir / "graph.json").write_text(json.dumps({"nodes": [], "edges": []}), encoding="utf-8")

    with pytest.raises(GraphifyOutputError, match="links"):
        validate_graphify_output(output_dir)


def test_build_knowledge_source_bundle_pairs_scoped_source_and_graph(tmp_path: Path):
    source_dir = tmp_path / "source"
    (source_dir / "src").mkdir(parents=True)
    (source_dir / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (source_dir / "README.md").write_text("not in configured scope\n", encoding="utf-8")
    (source_dir / ".git").mkdir()
    (source_dir / ".git" / "config").write_text("credential = secret\n", encoding="utf-8")
    graphify_dir = tmp_path / "working" / "graphify-out"
    _write_graphify_output(graphify_dir)
    graph = json.loads((graphify_dir / "graph.json").read_text(encoding="utf-8"))
    graph["nodes"][0]["source_file"] = "src/app.py"
    graph["nodes"].append({"id": "external:package", "name": "package", "source_file": "external/package"})
    (graphify_dir / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    manifest = json.loads((graphify_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["src/app.py"] = manifest.pop("app.py")
    (graphify_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    extraction = validate_graphify_output(graphify_dir)

    result = build_knowledge_source_bundle(
        source_id="4c199419-7a87-4e3c-93e8-411e7039a771",
        canonical_alias="org/example",
        repository_url="https://github.example.com/org/example.git",
        commit_sha="a" * 40,
        source_dir=source_dir,
        graphify=extraction,
        bundle_dir=tmp_path / "bundle",
        include_paths=["src/**"],
        exclude_paths=[],
        extraction_config_hash="b" * 64,
        extraction_stdout="Graph: 1 nodes, 0 edges\n",
    )

    assert result.file_count == 1
    assert result.node_count == 2
    assert set(result.artifact_paths) == {
        "graph",
        "source",
        "report",
        "manifest",
        "extraction_log",
    }
    assert verify_knowledge_source_bundle(result.bundle_dir)["source"]["commit_sha"] == "a" * 40
    bundled_graph = json.loads(result.artifact_paths["graph"].read_text(encoding="utf-8"))
    assert bundled_graph["nodes"][1]["source_file"] == ""

    with (
        result.artifact_paths["source"].open("rb") as compressed,
        zstandard.ZstdDecompressor().stream_reader(compressed) as reader,
        tarfile.open(fileobj=reader, mode="r|") as archive,
    ):
        assert [member.name for member in archive] == ["src/app.py"]


def test_build_knowledge_source_bundle_rejects_graph_outside_source_scope(tmp_path: Path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "app.py").write_text("pass\n", encoding="utf-8")
    graphify_dir = tmp_path / "working" / "graphify-out"
    _write_graphify_output(graphify_dir)
    extraction = validate_graphify_output(graphify_dir)

    with pytest.raises(KnowledgeSourceBundleError, match="configured source snapshot"):
        build_knowledge_source_bundle(
            source_id="4c199419-7a87-4e3c-93e8-411e7039a771",
            canonical_alias="org/example",
            repository_url="https://github.example.com/org/example.git",
            commit_sha="a" * 40,
            source_dir=source_dir,
            graphify=extraction,
            bundle_dir=tmp_path / "bundle",
            include_paths=["src/**"],
            exclude_paths=[],
            extraction_config_hash="b" * 64,
        )


def test_verify_knowledge_source_bundle_rejects_tampered_artifact(tmp_path: Path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "app.py").write_text("pass\n", encoding="utf-8")
    graphify_dir = tmp_path / "working" / "graphify-out"
    _write_graphify_output(graphify_dir)
    extraction = validate_graphify_output(graphify_dir)
    result = build_knowledge_source_bundle(
        source_id="4c199419-7a87-4e3c-93e8-411e7039a771",
        canonical_alias="org/example",
        repository_url="https://github.example.com/org/example.git",
        commit_sha="a" * 40,
        source_dir=source_dir,
        graphify=extraction,
        bundle_dir=tmp_path / "bundle",
        include_paths=[],
        exclude_paths=[],
        extraction_config_hash="b" * 64,
    )
    tampered_graph = bytearray(result.artifact_paths["graph"].read_bytes())
    tampered_graph[-2] ^= 1
    result.artifact_paths["graph"].write_bytes(tampered_graph)

    with pytest.raises(KnowledgeSourceBundleError, match="checksum"):
        verify_knowledge_source_bundle(result.bundle_dir)
