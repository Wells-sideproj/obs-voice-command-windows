from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

import pytest

from tools.post_merge_publisher import (
    CANONICAL_REPOSITORY,
    PAIRED_COMMENT_DIAGNOSTIC_ISSUE_NUMBER,
    PAIRED_COMMENT_DIAGNOSTIC_MARKER,
    PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER,
    ApiError,
    GithubApi,
    HttpResponse,
    PairedCommentDiagnosticResult,
    PostMergePublisher,
    PublisherConfig,
    PublisherError,
    parse_paired_comment_diagnostic_registration,
)


ROOT = Path(__file__).resolve().parents[3]
SHA = "a" * 40
DIAGNOSTIC_PR = 42
SOURCE_RUN = 9001
WORKFLOW_ID = 77
CHECK_SUITE_ID = 501
BOT = {"login": "github-actions[bot]", "type": "Bot"}
REPOSITORY_PATH = f"/repos/{CANONICAL_REPOSITORY}"


def _manifest(pr_number: int = DIAGNOSTIC_PR) -> str:
    return "\n".join(
        [
            "schema_version: 6",
            "paired_comment_diagnostic_registration:",
            "  version: 1",
            f"  pr_number: {pr_number}",
        ]
    )


def _payload_from_body(body: str) -> dict[str, Any]:
    match = re.search(r"```json\n(.*?)\n```", body, flags=re.DOTALL)
    assert match is not None
    value = json.loads(match.group(1))
    assert isinstance(value, dict)
    return value


class FakeTransport:
    def __init__(self, *, manifest: str | None = None) -> None:
        self.manifest = manifest if manifest is not None else _manifest()
        self.comments: dict[int, list[dict[str, Any]]] = {
            PAIRED_COMMENT_DIAGNOSTIC_ISSUE_NUMBER: [],
            PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER: [],
        }
        self.calls: list[tuple[str, str, dict[str, str | int] | None, dict[str, Any] | None]] = []
        self.next_comment_id = 10_000
        self.source_run_attempt = 1
        self.producer_pass = True
        self.uncertain_post_with_write: set[int] = set()
        self.uncertain_post_without_write: set[int] = set()
        self.post_errors: dict[int, PublisherError] = {}

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        del headers
        self.calls.append((method, path, params, json_body))
        if method == "GET" and path == REPOSITORY_PATH:
            return HttpResponse(200, {"full_name": CANONICAL_REPOSITORY})
        if method == "GET" and path == f"{REPOSITORY_PATH}/actions/workflows/post-merge.yml":
            return HttpResponse(
                200,
                {"id": WORKFLOW_ID, "path": ".github/workflows/post-merge.yml"},
            )
        if method == "GET" and path == f"{REPOSITORY_PATH}/git/ref/heads/develop":
            return HttpResponse(200, {"object": {"sha": SHA}})
        if method == "GET" and path == f"{REPOSITORY_PATH}/contents/docs/plans/2026-08-17-windows-11-ticket-manifest.yml":
            return HttpResponse(
                200,
                {
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(self.manifest.encode("utf-8")).decode("ascii"),
                },
            )
        run_match = re.fullmatch(
            rf"{re.escape(REPOSITORY_PATH)}/actions/runs/{SOURCE_RUN}/attempts/(\d+)",
            path,
        )
        if method == "GET" and run_match:
            attempt = int(run_match.group(1))
            if attempt != self.source_run_attempt:
                raise AssertionError(f"unexpected source run attempt {attempt}")
            return HttpResponse(200, self._source_run(attempt))
        jobs_match = re.fullmatch(
            rf"{re.escape(REPOSITORY_PATH)}/actions/runs/{SOURCE_RUN}/attempts/(\d+)/jobs",
            path,
        )
        if method == "GET" and jobs_match:
            attempt = int(jobs_match.group(1))
            if attempt != self.source_run_attempt:
                raise AssertionError(f"unexpected jobs run attempt {attempt}")
            return HttpResponse(200, {"jobs": self._jobs()})
        if method == "GET" and path == f"{REPOSITORY_PATH}/commits/{SHA}/check-suites":
            return HttpResponse(
                200,
                {
                    "check_suites": [
                        {
                            "id": CHECK_SUITE_ID,
                            "head_sha": SHA,
                            "conclusion": "success" if self.producer_pass else "failure",
                            "app": {"slug": "github-actions"},
                        }
                    ]
                },
            )
        if method == "GET" and path == f"{REPOSITORY_PATH}/commits/{SHA}/pulls":
            return HttpResponse(200, [{"number": DIAGNOSTIC_PR}])
        pull_match = re.fullmatch(rf"{re.escape(REPOSITORY_PATH)}/pulls/(\d+)", path)
        if method == "GET" and pull_match:
            return HttpResponse(200, self._pull(int(pull_match.group(1))))
        if method == "GET" and path == f"{REPOSITORY_PATH}/issues/{PAIRED_COMMENT_DIAGNOSTIC_ISSUE_NUMBER}":
            return HttpResponse(
                200,
                {
                    "number": PAIRED_COMMENT_DIAGNOSTIC_ISSUE_NUMBER,
                    "state": "closed",
                    "html_url": f"https://github.com/{CANONICAL_REPOSITORY}/issues/{PAIRED_COMMENT_DIAGNOSTIC_ISSUE_NUMBER}",
                },
            )
        comment_match = re.fullmatch(
            rf"{re.escape(REPOSITORY_PATH)}/issues/(\d+)/comments",
            path,
        )
        if comment_match:
            number = int(comment_match.group(1))
            if method == "GET":
                return HttpResponse(200, list(self.comments.setdefault(number, [])))
            if method == "POST":
                assert json_body is not None
                if number in self.post_errors:
                    raise self.post_errors[number]
                if number in self.uncertain_post_without_write:
                    self.uncertain_post_without_write.remove(number)
                    raise ApiError("POST", path, 503, "Internal Server Error")
                comment = {
                    "id": self.next_comment_id,
                    "body": json_body["body"],
                    "user": BOT,
                }
                self.next_comment_id += 1
                if number not in self.uncertain_post_with_write:
                    self.comments.setdefault(number, []).append(comment)
                else:
                    self.uncertain_post_with_write.remove(number)
                    self.comments.setdefault(number, []).append(comment)
                    raise PublisherError("transport ended after comment write", category="infrastructure")
                return HttpResponse(201, dict(comment))
        raise AssertionError(f"unexpected fake request: {method} {path}")

    def _source_run(self, attempt: int) -> dict[str, Any]:
        return {
            "id": SOURCE_RUN,
            "run_attempt": attempt,
            "head_sha": SHA,
            "workflow_id": WORKFLOW_ID,
            "event": "push",
            "head_branch": "develop",
            "status": "completed",
            "conclusion": "success" if self.producer_pass else "failure",
            "check_suite_id": CHECK_SUITE_ID,
            "html_url": f"https://github.com/{CANONICAL_REPOSITORY}/actions/runs/{SOURCE_RUN}",
            "repository": {"full_name": CANONICAL_REPOSITORY},
        }

    def _jobs(self) -> list[dict[str, Any]]:
        jobs = [
            {"name": name, "conclusion": "success", "steps": []}
            for name in (
                "layer-a / windows-unit",
                "layer-a / macos-regression",
                "layer-a / package",
                "required / gate",
            )
        ]
        if not self.producer_pass:
            jobs[0] = {
                "name": "layer-a / windows-unit",
                "conclusion": "failure",
                "steps": [{"name": "Run Windows hardware-free tests", "conclusion": "failure"}],
            }
        return jobs

    @staticmethod
    def _pull(number: int) -> dict[str, Any]:
        return {
            "number": number,
            "state": "closed",
            "merged_at": "2026-10-04T00:00:00Z",
            "merge_commit_sha": SHA if number == DIAGNOSTIC_PR else "b" * 40,
            "html_url": f"https://github.com/{CANONICAL_REPOSITORY}/pull/{number}",
            "base": {
                "ref": "develop",
                "repo": {"full_name": CANONICAL_REPOSITORY},
            },
        }


def _publisher(fake: FakeTransport) -> PostMergePublisher:
    return PostMergePublisher(
        GithubApi(fake, CANONICAL_REPOSITORY),
        PublisherConfig(repository=CANONICAL_REPOSITORY),
    )


def _post_count(fake: FakeTransport, number: int) -> int:
    return sum(
        method == "POST" and path == f"{REPOSITORY_PATH}/issues/{number}/comments"
        for method, path, _, _ in fake.calls
    )


def test_registration_parser_defaults_dormant_without_registration() -> None:
    assert parse_paired_comment_diagnostic_registration("schema_version: 6\n") is None


def test_registration_parser_rejects_placeholder_and_extra_fields() -> None:
    with pytest.raises(PublisherError, match="pr_number is invalid"):
        parse_paired_comment_diagnostic_registration(
            "paired_comment_diagnostic_registration:\n  version: 1\n  pr_number: <real PR>\n"
        )
    with pytest.raises(PublisherError, match="unexpected schema"):
        parse_paired_comment_diagnostic_registration(
            "paired_comment_diagnostic_registration:\n  version: 1\n  pr_number: 42\n  producer_run_id: 9001\n"
        )


def test_active_registration_writes_one_comment_per_closed_target_only() -> None:
    fake = FakeTransport()

    result = _publisher(fake).reconcile_diagnostic_or_normal(
        SOURCE_RUN,
        1,
        event_name="workflow_run",
    )

    assert isinstance(result, PairedCommentDiagnosticResult)
    assert set(result.created_targets) == {"issue#17", "pull_request#13"}
    assert len(fake.comments[17]) == 1
    assert len(fake.comments[13]) == 1
    assert all(
        PAIRED_COMMENT_DIAGNOSTIC_MARKER in comment["body"]
        for comments in fake.comments.values()
        for comment in comments
    )
    assert all(
        payload["production_projection"] is False
        for comments in fake.comments.values()
        for comment in comments
        for payload in [_payload_from_body(comment["body"])]
    )
    assert all(
        not (method == "POST" and path.endswith("/issues"))
        and not method in {"PATCH", "PUT"}
        for method, path, _, _ in fake.calls
    )
    assert _post_count(fake, 17) == 1
    assert _post_count(fake, 13) == 1


def test_matching_registration_with_failing_producer_keeps_normal_path() -> None:
    fake = FakeTransport()
    fake.producer_pass = False
    publisher = _publisher(fake)
    normal_calls: list[tuple[int | None, int | None]] = []

    def normal(run_id: int | None, attempt: int | None) -> list[Any]:
        normal_calls.append((run_id, attempt))
        return []

    publisher.reconcile_all = normal  # type: ignore[method-assign]
    result = publisher.reconcile_diagnostic_or_normal(
        SOURCE_RUN,
        1,
        event_name="workflow_run",
    )

    assert result == []
    assert normal_calls == [(SOURCE_RUN, 1)]
    assert _post_count(fake, 17) == 0
    assert _post_count(fake, 13) == 0
    assert fake.comments[17] == []
    assert fake.comments[13] == []


def test_rerun_reads_existing_pair_without_reposting() -> None:
    fake = FakeTransport()
    publisher = _publisher(fake)
    publisher.reconcile_diagnostic_or_normal(SOURCE_RUN, 1, event_name="workflow_run")
    before = (_post_count(fake, 17), _post_count(fake, 13))

    result = publisher.reconcile_diagnostic_or_normal(
        SOURCE_RUN,
        1,
        event_name="workflow_run",
    )

    assert isinstance(result, PairedCommentDiagnosticResult)
    assert result.created_targets == ()
    assert (_post_count(fake, 17), _post_count(fake, 13)) == before == (1, 1)


def test_uncertain_post_is_read_back_without_a_retry() -> None:
    fake = FakeTransport()
    fake.uncertain_post_with_write.add(17)

    result = _publisher(fake).reconcile_diagnostic_or_normal(
        SOURCE_RUN,
        1,
        event_name="workflow_run",
    )

    assert isinstance(result, PairedCommentDiagnosticResult)
    assert _post_count(fake, 17) == 1
    assert len(fake.comments[17]) == 1
    assert _post_count(fake, 13) == 1


def test_partial_pair_fences_rerun_after_pr_forbidden_without_reposting() -> None:
    fake = FakeTransport()
    fake.post_errors[13] = ApiError(
        "POST",
        f"{REPOSITORY_PATH}/issues/13/comments",
        403,
        "status=403; message=Forbidden; "
        "X-GitHub-Request-Id=abcd:123456:abcdef:7890ab:12345678; "
        "X-Accepted-GitHub-Permissions=issues=write, contents=read; "
        "Authorization=COMMENT_TOKEN_SENTINEL; body=COMMENT_BODY_SENTINEL",
    )
    publisher = _publisher(fake)

    with pytest.raises(PublisherError) as first:
        publisher.reconcile_diagnostic_or_normal(
            SOURCE_RUN,
            1,
            event_name="workflow_run",
        )

    first_message = str(first.value)
    assert "target=pull_request#13" in first_message
    assert "HTTP 403" in first_message
    assert "X-GitHub-Request-Id=abcd:123456:abcdef:7890ab:12345678" in first_message
    assert "X-Accepted-GitHub-Permissions=issues=write, contents=read" in first_message
    assert "COMMENT_TOKEN_SENTINEL" not in first_message
    assert "COMMENT_BODY_SENTINEL" not in first_message
    assert _post_count(fake, 17) == 1
    assert _post_count(fake, 13) == 1

    with pytest.raises(PublisherError, match="refusing all further POSTs"):
        publisher.reconcile_diagnostic_or_normal(
            SOURCE_RUN,
            1,
            event_name="workflow_run",
        )

    assert _post_count(fake, 17) == 1
    assert _post_count(fake, 13) == 1
    assert len(fake.comments[17]) == 1
    assert fake.comments[13] == []

    # A producer rerun changes the run attempt but must still see Issue #17 as
    # the persistent fence; it cannot try the missing PR target again.
    fake.source_run_attempt = 2
    with pytest.raises(PublisherError, match="refusing all further POSTs"):
        publisher.reconcile_diagnostic_or_normal(
            SOURCE_RUN,
            2,
            event_name="workflow_run",
        )

    assert _post_count(fake, 17) == 1
    assert _post_count(fake, 13) == 1


def test_uncertain_post_without_readback_stops_without_posting_second_target() -> None:
    fake = FakeTransport()
    fake.uncertain_post_without_write.add(17)

    with pytest.raises(PublisherError, match="not confirmed by read-back"):
        _publisher(fake).reconcile_diagnostic_or_normal(
            SOURCE_RUN,
            1,
            event_name="workflow_run",
        )

    assert _post_count(fake, 17) == 1
    assert _post_count(fake, 13) == 0
    assert fake.comments[17] == []


def test_diagnostic_is_not_active_for_manual_context() -> None:
    fake = FakeTransport()
    publisher = _publisher(fake)
    called: list[tuple[int | None, int | None]] = []

    def normal(run_id: int | None, attempt: int | None) -> list[Any]:
        called.append((run_id, attempt))
        return []

    publisher.reconcile_all = normal  # type: ignore[method-assign]
    result = publisher.reconcile_diagnostic_or_normal(
        SOURCE_RUN,
        1,
        event_name="workflow_dispatch",
    )

    assert result == []
    assert called == [(SOURCE_RUN, 1)]
    assert fake.comments[17] == []
    assert fake.comments[13] == []


def test_dormant_registration_preserves_normal_reconciliation() -> None:
    fake = FakeTransport(manifest="schema_version: 6\n")
    publisher = _publisher(fake)
    called: list[tuple[int | None, int | None]] = []

    def normal(run_id: int | None, attempt: int | None) -> list[Any]:
        called.append((run_id, attempt))
        return []

    publisher.reconcile_all = normal  # type: ignore[method-assign]
    result = publisher.reconcile_diagnostic_or_normal(
        SOURCE_RUN,
        1,
        event_name="workflow_run",
    )

    assert result == []
    assert called == [(SOURCE_RUN, 1)]
    assert fake.comments[17] == []
    assert fake.comments[13] == []


def test_finalizer_uses_same_job_and_keeps_original_permissions() -> None:
    workflow = (ROOT / ".github" / "workflows" / "post-merge-finalize.yml").read_text(
        encoding="utf-8"
    )
    jobs = workflow.split("jobs:\n", 1)[1]

    assert workflow.count("jobs:\n") == 1
    assert "run: python tools/post_merge_publisher.py --diagnostic-aware" in jobs
    assert "contents: read" in jobs
    assert "actions: read" in jobs
    assert "checks: read" in jobs
    assert "pull-requests: read" in jobs
    assert "issues: write" in jobs
    assert "contents: write" not in jobs
    assert "actions: write" not in jobs
    assert "checks: write" not in jobs
    assert "pull-requests: write" not in jobs
    assert "PAT" not in workflow
    assert "GCM" not in workflow
    assert "continue-on-error" not in workflow
