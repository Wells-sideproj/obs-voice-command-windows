from __future__ import annotations

import base64
import io
import json
import os
import sys
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.post_merge_publisher import (
    CANONICAL_REPOSITORY,
    FINALIZER_JOB,
    GithubApi,
    HttpResponse,
    PostMergePublisher,
    PublisherConfig,
    PublisherError,
    _cli,
    parse_paired_comment_diagnostic_registration,
    parse_pr_comment_authorization_continuation_registration,
    parse_production_publication_pause,
)


ROOT = Path(__file__).resolve().parents[3]
SHA = "a" * 40
SOURCE_RUN = 9001
SOURCE_PR = 42
WORKFLOW_ID = 77
CHECK_SUITE_ID = 501
REPOSITORY_PATH = f"/repos/{CANONICAL_REPOSITORY}"


def _pause_manifest() -> str:
    return "\n".join(
        [
            "schema_version: 6",
            "production_publication_pause:",
            "  state: blocked",
            "  reason: pending-authorization",
            "  durable_permission: awaiting_explicit_authorization",
            "  diagnostic_state: consumed",
            "  production_post: disabled",
            "  production_patch: disabled",
            "  production_delete: disabled",
            "  retired_registrations:",
            "    - paired_comment_diagnostic_registration (historical version 1)",
            "    - pr_comment_authorization_continuation_registration (historical version 1)",
            "  historical_evidence:",
            "    issue_number: 17",
            "    pr_number: 13",
            "    durable_pr_number: 19",
            "    finalizer_workflow_id: 373741433",
            "    finalizer_run_id: 37281697373",
            "    finalizer_run_number: 5",
            "    finalizer_run_attempt: 1",
            "    producer_run_id: 37281618408",
            "    producer_sha: 27c8fb321c7dc5bcc7280197d46dd7c44fdb1d7b",
            "    pr13_comment_id: 5990563780",
        ]
    )


class FakeTransport:
    """GET-only transport proving the paused finalizer cannot mutate."""

    def __init__(self, *, manifest: str | None = None) -> None:
        self.manifest = manifest if manifest is not None else _pause_manifest()
        self.protected_tip = SHA
        self.calls: list[tuple[str, str, dict[str, str | int] | None, dict[str, object] | None]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        json_body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        del headers
        self.calls.append((method, path, params, json_body))
        if method != "GET":
            raise AssertionError(f"paused production path attempted {method} {path}")
        if path == REPOSITORY_PATH:
            return HttpResponse(200, {"full_name": CANONICAL_REPOSITORY})
        if path == f"{REPOSITORY_PATH}/git/ref/heads/develop":
            return HttpResponse(200, {"object": {"sha": self.protected_tip}})
        if path == f"{REPOSITORY_PATH}/contents/docs/plans/2026-08-17-windows-11-ticket-manifest.yml":
            return HttpResponse(
                200,
                {
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(self.manifest.encode("utf-8")).decode("ascii"),
                },
            )
        if path == f"{REPOSITORY_PATH}/actions/workflows/post-merge.yml":
            return HttpResponse(
                200,
                {"id": WORKFLOW_ID, "path": ".github/workflows/post-merge.yml"},
            )
        if path == f"{REPOSITORY_PATH}/actions/runs/{SOURCE_RUN}/attempts/1":
            return HttpResponse(200, self._source_run())
        if path == f"{REPOSITORY_PATH}/actions/runs/{SOURCE_RUN}/attempts/1/jobs":
            return HttpResponse(200, {"jobs": self._jobs()})
        if path == f"{REPOSITORY_PATH}/commits/{SHA}/check-suites":
            return HttpResponse(
                200,
                {
                    "check_suites": [
                        {
                            "id": CHECK_SUITE_ID,
                            "head_sha": SHA,
                            "conclusion": "success",
                            "app": {"slug": "github-actions"},
                        }
                    ]
                },
            )
        if path == f"{REPOSITORY_PATH}/commits/{SHA}/pulls":
            return HttpResponse(200, [{"number": SOURCE_PR}])
        if path == f"{REPOSITORY_PATH}/pulls/{SOURCE_PR}":
            return HttpResponse(
                200,
                {
                    "number": SOURCE_PR,
                    "state": "closed",
                    "merged_at": "2026-10-05T00:00:00Z",
                    "merge_commit_sha": SHA,
                    "html_url": f"https://github.com/{CANONICAL_REPOSITORY}/pull/{SOURCE_PR}",
                    "base": {
                        "ref": "develop",
                        "repo": {"full_name": CANONICAL_REPOSITORY},
                    },
                },
            )
        raise AssertionError(f"unexpected GET {path}")

    @staticmethod
    def _source_run() -> dict[str, object]:
        return {
            "id": SOURCE_RUN,
            "run_attempt": 1,
            "head_sha": SHA,
            "workflow_id": WORKFLOW_ID,
            "event": "push",
            "head_branch": "develop",
            "status": "completed",
            "conclusion": "success",
            "check_suite_id": CHECK_SUITE_ID,
            "html_url": f"https://github.com/{CANONICAL_REPOSITORY}/actions/runs/{SOURCE_RUN}",
            "repository": {"full_name": CANONICAL_REPOSITORY},
        }

    @staticmethod
    def _jobs() -> list[dict[str, object]]:
        return [
            {"name": name, "conclusion": "success", "steps": []}
            for name in (
                "layer-a / windows-unit",
                "layer-a / macos-regression",
                "layer-a / package",
                "required / gate",
            )
        ]


def _publisher(fake: FakeTransport, *, finalizer_run_number: int = 6) -> PostMergePublisher:
    return PostMergePublisher(
        GithubApi(fake, CANONICAL_REPOSITORY),
        PublisherConfig(
            repository=CANONICAL_REPOSITORY,
            finalizer_run_id=6006,
            finalizer_run_number=finalizer_run_number,
            finalizer_run_attempt=1,
            finalizer_job=FINALIZER_JOB,
        ),
    )


def _assert_pending(fake: FakeTransport, callback: object) -> None:
    with pytest.raises(PublisherError, match="production publication blocked/pending-authorization") as caught:
        callback()  # type: ignore[operator]
    assert caught.value.category == "pending-authorization"
    assert "zero production POST/PATCH/DELETE" in str(caught.value)
    assert all(method == "GET" for method, _, _, _ in fake.calls)


def test_real_manifest_has_one_pause_and_no_active_registration() -> None:
    manifest = (ROOT / "docs" / "plans" / "2026-08-17-windows-11-ticket-manifest.yml").read_text(
        encoding="utf-8"
    )
    pause = parse_production_publication_pause(manifest)
    assert pause is not None
    assert pause.state == "blocked"
    assert pause.reason == "pending-authorization"
    assert pause.durable_permission == "awaiting_explicit_authorization"
    assert pause.diagnostic_state == "consumed"
    assert manifest.count("production_publication_pause:\n") == 1
    active_keys = {
        "paired_comment_diagnostic_registration:",
        "pr_comment_authorization_continuation_registration:",
    }
    assert not any(line in active_keys for line in manifest.splitlines())
    for evidence in (
        "durable_pr_number: 19",
        "finalizer_run_id: 37281697373",
        "producer_run_id: 37281618408",
        "producer_sha: 27c8fb321c7dc5bcc7280197d46dd7c44fdb1d7b",
        "pr13_comment_id: 5990563780",
    ):
        assert manifest.count(evidence) == 1


def test_cleanup_keeps_w11_010_blocked_and_retry_state_unchanged() -> None:
    manifest = (ROOT / "docs" / "plans" / "2026-08-17-windows-11-ticket-manifest.yml").read_text(
        encoding="utf-8"
    )
    w11 = manifest.split("  - id: W11-010", 1)[1].split("  - id: W11-011", 1)[0]
    assert "    status: Blocked" in w11
    assert "      code_retry_budget_consumed: false" in w11
    assert "      state: none" in w11
    assert "      issue_number: 14" in w11


def test_workflow_run_run_six_verifies_source_then_fails_pending_without_mutation() -> None:
    fake = FakeTransport()
    _assert_pending(
        fake,
        lambda: _publisher(fake, finalizer_run_number=6).reconcile_diagnostic_or_normal(
            SOURCE_RUN,
            1,
            event_name="workflow_run",
        ),
    )
    paths = [path for _, path, _, _ in fake.calls]
    assert f"{REPOSITORY_PATH}/actions/workflows/post-merge.yml" in paths
    assert f"{REPOSITORY_PATH}/commits/{SHA}/pulls" in paths
    assert not any("post-merge-finalize.yml" in path for path in paths)
    assert not any("/issues/14" in path for path in paths)


def test_workflow_dispatch_normal_reconciliation_is_pending_and_zero_mutation() -> None:
    fake = FakeTransport()
    _assert_pending(
        fake,
        lambda: _publisher(fake).reconcile_diagnostic_or_normal(
            SOURCE_RUN,
            1,
            event_name="workflow_dispatch",
        ),
    )


def test_old_marker_redelivery_does_not_rearm_or_mutate() -> None:
    fake = FakeTransport()
    publisher = _publisher(fake)
    for _ in range(2):
        _assert_pending(
            fake,
            lambda: publisher.reconcile_diagnostic_or_normal(
                SOURCE_RUN,
                1,
                event_name="workflow_run",
            ),
        )
    assert not any(method != "GET" for method, _, _, _ in fake.calls)
    assert not any("/issues/14" in path for _, path, _, _ in fake.calls)


def test_no_pause_fails_closed_in_both_normal_event_contexts() -> None:
    fake = FakeTransport(manifest="schema_version: 6\n")
    publisher = _publisher(fake)
    for event_name in ("workflow_run", "workflow_dispatch"):
        with pytest.raises(PublisherError, match="pause is missing") as caught:
            publisher.reconcile_diagnostic_or_normal(None, None, event_name=event_name)
        assert caught.value.category == "pending-authorization"
    assert all(method == "GET" for method, _, _, _ in fake.calls)


def test_duplicate_pause_child_fails_closed_before_source_verification() -> None:
    malformed = _pause_manifest().replace(
        "  state: blocked\n",
        "  state: blocked\n  state: blocked\n",
    )
    fake = FakeTransport(manifest=malformed)
    with pytest.raises(PublisherError, match="duplicate state"):
        _publisher(fake).reconcile_diagnostic_or_normal(SOURCE_RUN, 1, event_name="workflow_run")
    assert all(method == "GET" for method, _, _, _ in fake.calls)


def test_duplicate_pause_blocks_fail_closed() -> None:
    malformed = _pause_manifest() + "\nproduction_publication_pause:\n"
    with pytest.raises(PublisherError, match="duplicate production publication pause"):
        parse_production_publication_pause(malformed)


def test_retired_registration_cannot_be_rearmed_even_with_pause() -> None:
    fake = FakeTransport(
        manifest=(
            "paired_comment_diagnostic_registration:\n"
            "  version: 1\n"
            "  pr_number: 13\n"
            + _pause_manifest()
        )
    )
    with pytest.raises(PublisherError, match="retired"):
        _publisher(fake).reconcile_diagnostic_or_normal(None, None, event_name="workflow_run")
    assert all(method == "GET" for method, _, _, _ in fake.calls)
    with pytest.raises(PublisherError, match="retired"):
        parse_paired_comment_diagnostic_registration(
            "paired_comment_diagnostic_registration:\n  version: 1\n  pr_number: 13\n"
        )
    with pytest.raises(PublisherError, match="retired"):
        parse_pr_comment_authorization_continuation_registration(
            "pr_comment_authorization_continuation_registration:\n"
            "  version: 1\n  pr_number: 19\n"
        )


def test_cli_reports_nonzero_pending_authorization_for_finalizer_run_six() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "GITHUB_REPOSITORY": CANONICAL_REPOSITORY,
            "GITHUB_TOKEN": "unused-test-token",
            "GITHUB_EVENT_NAME": "workflow_run",
            "GITHUB_REF": "refs/heads/develop",
            "GITHUB_RUN_ID": "37281697373",
            "GITHUB_RUN_NUMBER": "6",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_JOB": FINALIZER_JOB,
            "POST_MERGE_RUN_ID": str(SOURCE_RUN),
            "POST_MERGE_RUN_ATTEMPT": "1",
        }
    )
    stderr = io.StringIO()
    with (
        patch.dict(os.environ, environment, clear=True),
        patch.object(
            PostMergePublisher,
            "reconcile_diagnostic_or_normal",
            side_effect=PublisherError(
                "production publication blocked/pending-authorization; zero production POST/PATCH/DELETE",
                category="pending-authorization",
            ),
        ),
        patch.object(sys, "argv", ["post_merge_publisher.py"]),
        redirect_stderr(stderr),
    ):
        result = _cli()
    assert result == 1
    payload = json.loads(stderr.getvalue())
    assert payload["status"] == "blocked"
    assert payload["category"] == "pending-authorization"
    assert "zero production POST/PATCH/DELETE" in payload["error"]
