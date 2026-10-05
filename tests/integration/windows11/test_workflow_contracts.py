from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
FINALIZER = ROOT / ".github" / "workflows" / "post-merge-finalize.yml"
LAYER_B = ROOT / ".github" / "workflows" / "windows11-integration.yml"
REPAIR_FORM = ROOT / ".github" / "ISSUE_TEMPLATE" / "repair.yml"


def _header(path: Path) -> str:
    return path.read_text(encoding="utf-8").split("jobs:\n", 1)[0]


def test_finalizer_is_a_distinct_serialized_workflow_run_listener() -> None:
    workflow = FINALIZER.read_text(encoding="utf-8")
    header = _header(FINALIZER)

    assert "name: Windows 11 Layer A post-merge finalizer" in workflow
    assert "workflow_run:" in header
    assert "workflows:\n      - Windows 11 Layer A post-merge producer" in header
    assert "types:\n      - completed" in header
    assert "branches:\n      - develop" in header
    assert "workflow_dispatch:" in header
    assert "pull_request:" not in header
    assert "merge_group:" not in header
    assert "push:" not in header
    assert "permissions: {}" in header
    assert "group: w11-010-post-merge-finalizer" in header
    assert "cancel-in-progress: false" in header
    assert "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/develop'" in workflow
    assert "simulation_case:" in header
    assert "- pass" in header
    assert "- fail" in header
    assert "- rerun" in header
    assert "POST_MERGE_SIMULATION_CASE" in workflow
    assert "--simulation-case" in workflow
    assert "w11-010-simulation" not in header
    assert "github.event_name == 'workflow_run' || inputs.simulation_case == ''" in workflow


def test_finalizer_has_only_the_arbitrated_mutation_permissions() -> None:
    workflow = FINALIZER.read_text(encoding="utf-8")
    job = workflow.split("jobs:\n", 1)[1]
    assert "runs-on: ubuntu-latest" in job
    assert "contents: read" in job
    assert "actions: read" in job
    assert "checks: read" in job
    assert "pull-requests: write" in job
    assert "issues: write" in job
    assert "contents: write" not in job
    assert "actions: write" not in job
    assert "checks: write" not in job
    assert "pull-requests: read" not in job
    assert job.count("pull-requests: write") == 1
    assert "ref: develop" in job
    assert "tools/post_merge_publisher.py" in job
    assert "github.event.workflow_run.head_sha" not in job
    assert "production ticket state" in workflow


def test_layer_b_workflow_is_inactive_until_manual_or_reusable_authorized_call() -> None:
    workflow = LAYER_B.read_text(encoding="utf-8")
    header = _header(LAYER_B)
    assert "workflow_dispatch:" in header
    assert "workflow_call:" in header
    assert "push:" not in header
    assert "pull_request:" not in header
    assert "merge_group:" not in header
    assert "permissions: {}" in header
    assert "group: w11-010-obs-integration" in header
    assert "cancel-in-progress: false" in header
    assert "self-hosted, Windows, X64, obs-integration" in workflow
    assert "name: windows11-integration / obs-e2e" in workflow
    assert "needs: preflight" in workflow
    assert "protected_sha" in workflow
    assert "W11_010_HARDWARE_AUTHORIZATION" in workflow
    assert "tools/windows11_integration_harness.py" in workflow
    assert "--host" not in workflow


def test_repair_form_captures_controller_evidence_without_secrets() -> None:
    form = REPAIR_FORM.read_text(encoding="utf-8")
    for field in ("ticket-id", "failing-layer", "commit-sha", "run-url", "merged-pr", "evidence", "rollback-hint"):
        assert f"id: {field}" in form
    assert "OBS_WEBSOCKET_PASSWORD" not in form
    assert "token" in form.lower()
