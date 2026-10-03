from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
POST_MERGE_WORKFLOW = ROOT / ".github" / "workflows" / "post-merge.yml"


def _jobs_block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    return text.split("jobs:\n", 1)[1]


def _environment_block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    return text.split("env:\n", 1)[1].split("\njobs:\n", 1)[0]


def test_post_merge_producer_copies_the_complete_layer_a_job_graph() -> None:
    """The producer is a literal suite copy; ci.yml remains its parity oracle."""

    assert _jobs_block(POST_MERGE_WORKFLOW) == _jobs_block(CI_WORKFLOW)
    assert _environment_block(POST_MERGE_WORKFLOW) == _environment_block(CI_WORKFLOW)


def test_post_merge_trigger_and_permissions_are_trusted_push_only() -> None:
    workflow = POST_MERGE_WORKFLOW.read_text(encoding="utf-8")
    header = workflow.split("jobs:\n", 1)[0]

    assert "push:\n    branches:\n      - develop" in header
    assert "pull_request" not in header
    assert "merge_group" not in header
    assert "workflow_run" not in header
    assert "permissions:\n  contents: read" in header
    assert "issues:" not in header
    assert "actions:" not in header
    assert "checks:" not in header
    assert "pull-requests:" not in header
    assert "self-hosted" not in workflow.lower()


def test_post_merge_checkout_uses_the_trusted_push_head() -> None:
    """A push run must inspect its event SHA, never a caller-selected ref."""

    jobs = _jobs_block(POST_MERGE_WORKFLOW)
    assert "actions/checkout@" in jobs
    assert "persist-credentials: false" in jobs
    assert "\n        ref:" not in jobs
    assert "github.event.pull_request" not in jobs
    assert "github.event.workflow_run" not in jobs
