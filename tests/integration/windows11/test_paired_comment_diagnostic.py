from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

import pytest

from tools.post_merge_publisher import (
    CANONICAL_REPOSITORY,
    FINALIZER_JOB,
    FINALIZER_RUN_ATTEMPT,
    FINALIZER_RUN_NUMBER,
    FINALIZER_WORKFLOW_ID,
    FINALIZER_WORKFLOW_PATH,
    OLD_PAIRED_MARKER_COMMENT_ID,
    OLD_PAIRED_SOURCE_PR_NUMBER,
    OLD_PAIRED_SOURCE_RUN_ATTEMPT,
    OLD_PAIRED_SOURCE_RUN_ID,
    OLD_PAIRED_SOURCE_SHA,
    PAIRED_COMMENT_DIAGNOSTIC_ISSUE_NUMBER,
    PAIRED_COMMENT_DIAGNOSTIC_MARKER,
    PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER,
    PR_COMMENT_AUTHORIZATION_CONTINUATION_MARKER,
    ApiError,
    FinalizerIdentity,
    GithubApi,
    HttpResponse,
    PairedCommentDiagnostic,
    PairedCommentDiagnosticRegistration,
    PairedCommentDiagnosticResult,
    PrCommentAuthorizationContinuation,
    PrCommentAuthorizationContinuationRegistration,
    PrCommentAuthorizationContinuationResult,
    PostMergePublisher,
    PublisherConfig,
    PublisherError,
    VerifiedPull,
    VerifiedRun,
    parse_paired_comment_diagnostic_registration,
    parse_production_publication_pause,
    parse_pr_comment_authorization_continuation_registration,
)


ROOT = Path(__file__).resolve().parents[3]
SHA = "a" * 40
DIAGNOSTIC_PR = 42
SOURCE_RUN = 9001
WORKFLOW_ID = 77
CHECK_SUITE_ID = 501
FINALIZER_RUN = 5005
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


def _continuation_manifest(pr_number: int = DIAGNOSTIC_PR) -> str:
    return "\n".join(
        [
            _manifest(pr_number),
            "pr_comment_authorization_continuation_registration:",
            "  version: 1",
            f"  pr_number: {pr_number}",
        ]
    )


def _old_issue_marker_body() -> str:
    payload = {
        "kind": "w11-010-paired-comment-diagnostic",
        "version": 1,
        "namespace": "w11-010-paired-comment-diagnostic",
        "repository": CANONICAL_REPOSITORY,
        "production_projection": False,
        "source_pr_number": OLD_PAIRED_SOURCE_PR_NUMBER,
        "source_sha": OLD_PAIRED_SOURCE_SHA,
        "source_run_id": OLD_PAIRED_SOURCE_RUN_ID,
        "source_run_attempt": OLD_PAIRED_SOURCE_RUN_ATTEMPT,
        "target_kind": "issue",
        "target_number": PAIRED_COMMENT_DIAGNOSTIC_ISSUE_NUMBER,
    }
    return "\n".join(
        [
            PAIRED_COMMENT_DIAGNOSTIC_MARKER,
            "```json",
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            "```",
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
        self.finalizer_run_id = FINALIZER_RUN
        self.finalizer_run_number = FINALIZER_RUN_NUMBER
        self.finalizer_run_attempt = FINALIZER_RUN_ATTEMPT
        self.finalizer_workflow_id = FINALIZER_WORKFLOW_ID
        self.finalizer_event = "workflow_run"
        self.finalizer_head_branch = "develop"
        self.finalizer_repository = CANONICAL_REPOSITORY
        self.finalizer_status = "in_progress"
        self.finalizer_conclusion: str | None = None
        self.finalizer_path = FINALIZER_WORKFLOW_PATH
        self.finalizer_job = FINALIZER_JOB
        self.protected_tip = SHA
        self.workflow_runs: list[dict[str, Any]] = []
        self.pr13_locked = False
        self.hide_pr13_readback = False
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
        if method == "GET" and path == (
            f"{REPOSITORY_PATH}/actions/workflows/{FINALIZER_WORKFLOW_PATH.rsplit('/', 1)[-1]}"
        ):
            return HttpResponse(
                200,
                {"id": self.finalizer_workflow_id, "path": self.finalizer_path},
            )
        if method == "GET" and path == f"{REPOSITORY_PATH}/git/ref/heads/develop":
            return HttpResponse(200, {"object": {"sha": self.protected_tip}})
        if method == "GET" and path == f"{REPOSITORY_PATH}/contents/docs/plans/2026-08-17-windows-11-ticket-manifest.yml":
            return HttpResponse(
                200,
                {
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(self.manifest.encode("utf-8")).decode("ascii"),
                },
            )
        finalizer_run_match = re.fullmatch(
            rf"{re.escape(REPOSITORY_PATH)}/actions/runs/{self.finalizer_run_id}/attempts/(\d+)",
            path,
        )
        if method == "GET" and finalizer_run_match:
            attempt = int(finalizer_run_match.group(1))
            if attempt != FINALIZER_RUN_ATTEMPT:
                raise AssertionError(f"unexpected finalizer run attempt {attempt}")
            return HttpResponse(200, self._finalizer_run(attempt))
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
        workflow_runs_match = re.fullmatch(
            rf"{re.escape(REPOSITORY_PATH)}/actions/workflows/{FINALIZER_WORKFLOW_ID}/runs",
            path,
        )
        if method == "GET" and workflow_runs_match:
            return HttpResponse(200, {"workflow_runs": list(self.workflow_runs)})
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
                if number == PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER and self.hide_pr13_readback:
                    return HttpResponse(200, [])
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

    def _finalizer_run(self, attempt: int) -> dict[str, Any]:
        return {
            "id": self.finalizer_run_id,
            "run_attempt": attempt if self.finalizer_run_attempt == FINALIZER_RUN_ATTEMPT else self.finalizer_run_attempt,
            "run_number": self.finalizer_run_number,
            "workflow_id": self.finalizer_workflow_id,
            "event": self.finalizer_event,
            "head_branch": self.finalizer_head_branch,
            "status": self.finalizer_status,
            "conclusion": self.finalizer_conclusion,
            "path": self.finalizer_path,
            "html_url": f"https://github.com/{CANONICAL_REPOSITORY}/actions/runs/{self.finalizer_run_id}",
            "repository": {"full_name": self.finalizer_repository},
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

    def _pull(self, number: int) -> dict[str, Any]:
        return {
            "number": number,
            "state": "closed",
            "merged_at": "2026-10-04T00:00:00Z",
            "merge_commit_sha": (
                SHA
                if number == DIAGNOSTIC_PR
                else OLD_PAIRED_SOURCE_SHA
                if number == OLD_PAIRED_SOURCE_PR_NUMBER
                else "b" * 40
            ),
            "locked": self.pr13_locked if number == PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER else False,
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


def _continuation_publisher(fake: FakeTransport) -> PostMergePublisher:
    return PostMergePublisher(
        GithubApi(fake, CANONICAL_REPOSITORY),
        PublisherConfig(
            repository=CANONICAL_REPOSITORY,
            finalizer_run_id=fake.finalizer_run_id,
            finalizer_run_number=fake.finalizer_run_number,
            finalizer_run_attempt=fake.finalizer_run_attempt,
            finalizer_job=fake.finalizer_job,
        ),
    )


def _arm_old_issue_marker(fake: FakeTransport) -> None:
    fake.comments[PAIRED_COMMENT_DIAGNOSTIC_ISSUE_NUMBER] = [
        {
            "id": OLD_PAIRED_MARKER_COMMENT_ID,
            "body": _old_issue_marker_body(),
            "user": BOT,
        }
    ]


def _post_count(fake: FakeTransport, number: int) -> int:
    return sum(
        method == "POST" and path == f"{REPOSITORY_PATH}/issues/{number}/comments"
        for method, path, _, _ in fake.calls
    )


def _protected_pause_manifest() -> str:
    return (ROOT / "docs" / "plans" / "2026-08-17-windows-11-ticket-manifest.yml").read_text(
        encoding="utf-8"
    )


def _retired_manifest(registration_name: str, *, pr_number: int = DIAGNOSTIC_PR) -> str:
    return (
        _protected_pause_manifest().rstrip()
        + "\n"
        + f"{registration_name}:\n"
        + "  version: 1\n"
        + f"  pr_number: {pr_number}\n"
    )


def _assert_zero_mutation(fake: FakeTransport) -> None:
    assert _post_count(fake, PAIRED_COMMENT_DIAGNOSTIC_ISSUE_NUMBER) == 0
    assert _post_count(fake, PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER) == 0
    assert all(method == "GET" for method, _, _, _ in fake.calls)


def _assert_pending_authorization(
    fake: FakeTransport,
    *,
    event_name: str,
    publisher: PostMergePublisher | None = None,
    preferred_run_id: int | None = SOURCE_RUN,
    preferred_run_attempt: int | None = 1,
) -> None:
    with pytest.raises(PublisherError, match="pending-authorization"):
        (publisher or _publisher(fake)).reconcile_diagnostic_or_normal(
            preferred_run_id,
            preferred_run_attempt,
            event_name=event_name,
        )
    _assert_zero_mutation(fake)


def _assert_retired_registration(
    fake: FakeTransport,
    *,
    publisher: PostMergePublisher | None = None,
    event_name: str = "workflow_run",
) -> None:
    with pytest.raises(PublisherError, match="retired"):
        (publisher or _publisher(fake)).reconcile_diagnostic_or_normal(
            SOURCE_RUN,
            1,
            event_name=event_name,
        )
    _assert_zero_mutation(fake)


def _verified_run() -> VerifiedRun:
    return VerifiedRun(
        run_id=SOURCE_RUN,
        run_attempt=1,
        sha=SHA,
        workflow_id=WORKFLOW_ID,
        workflow_path=".github/workflows/post-merge.yml",
        url=f"https://github.com/{CANONICAL_REPOSITORY}/actions/runs/{SOURCE_RUN}",
        conclusion="success",
        quality="pass",
        check_suite_id=CHECK_SUITE_ID,
        provider="github-actions",
        job_conclusions={},
        failed_steps=(),
    )


def _verified_pull() -> VerifiedPull:
    return VerifiedPull(
        number=DIAGNOSTIC_PR,
        merge_sha=SHA,
        url=f"https://github.com/{CANONICAL_REPOSITORY}/pull/{DIAGNOSTIC_PR}",
    )


def _finalizer_identity() -> FinalizerIdentity:
    return FinalizerIdentity(
        run_id=FINALIZER_RUN,
        run_number=FINALIZER_RUN_NUMBER,
        run_attempt=FINALIZER_RUN_ATTEMPT,
        job=FINALIZER_JOB,
        workflow_id=FINALIZER_WORKFLOW_ID,
        workflow_path=FINALIZER_WORKFLOW_PATH,
        url=f"https://github.com/{CANONICAL_REPOSITORY}/actions/runs/{FINALIZER_RUN}",
    )


def test_registration_parser_defaults_dormant_without_registration() -> None:
    assert parse_paired_comment_diagnostic_registration("schema_version: 6\n") is None


def test_registration_parser_rejects_retired_registration_even_with_legacy_shape() -> None:
    with pytest.raises(PublisherError, match="retired"):
        parse_paired_comment_diagnostic_registration(
            "paired_comment_diagnostic_registration:\n  version: 1\n  pr_number: <real PR>\n"
        )
    with pytest.raises(PublisherError, match="retired"):
        parse_paired_comment_diagnostic_registration(
            "paired_comment_diagnostic_registration:\n  version: 1\n  pr_number: 42\n  producer_run_id: 9001\n"
        )


def test_continuation_registration_is_absent_or_retired() -> None:
    assert parse_pr_comment_authorization_continuation_registration("schema_version: 6\n") is None
    with pytest.raises(PublisherError, match="retired"):
        parse_pr_comment_authorization_continuation_registration(
            "pr_comment_authorization_continuation_registration:\n"
            "  version: 1\n"
            "  pr_number: <controller PR>\n"
        )
    with pytest.raises(PublisherError, match="retired"):
        parse_pr_comment_authorization_continuation_registration(
            "pr_comment_authorization_continuation_registration:\n"
            "  version: 1\n"
            "  pr_number: 42\n"
            "  run_number: 5\n"
        )


def test_retired_diagnostic_classes_fail_before_transport() -> None:
    fake = FakeTransport(manifest=_protected_pause_manifest())
    api = GithubApi(fake, CANONICAL_REPOSITORY)
    with pytest.raises(PublisherError, match="retired"):
        PairedCommentDiagnostic(
            api,
            PublisherConfig(repository=CANONICAL_REPOSITORY),
            PairedCommentDiagnosticRegistration(version=1, pr_number=DIAGNOSTIC_PR),
        ).run(_verified_run(), _verified_pull())
    with pytest.raises(PublisherError, match="retired"):
        PrCommentAuthorizationContinuation(
            api,
            PublisherConfig(repository=CANONICAL_REPOSITORY),
            PrCommentAuthorizationContinuationRegistration(version=1, pr_number=DIAGNOSTIC_PR),
            _finalizer_identity(),
        ).run(_verified_run(), _verified_pull())
    assert fake.calls == []


def test_real_protected_pause_manifest_is_parseable_without_active_registration() -> None:
    manifest = _protected_pause_manifest()
    pause = parse_production_publication_pause(manifest)
    assert pause is not None
    assert pause.state == "blocked"
    assert pause.reason == "pending-authorization"
    assert parse_paired_comment_diagnostic_registration(manifest) is None
    assert parse_pr_comment_authorization_continuation_registration(manifest) is None


def test_continuation_fixed_run_5_attempt_1_is_retired_without_listing_runs() -> None:
    fake = FakeTransport(
        manifest=_retired_manifest("pr_comment_authorization_continuation_registration")
    )
    _arm_old_issue_marker(fake)
    fake.workflow_runs = [{"id": 1, "run_number": 4}, {"id": 2, "run_number": 3}]

    _assert_retired_registration(fake, publisher=_continuation_publisher(fake))
    assert not any(
        "/actions/workflows/373741433/runs" in path
        for _, path, _, _ in fake.calls
    )


def test_continuation_success_replay_of_old_marker_stays_retired_without_reposting() -> None:
    fake = FakeTransport(
        manifest=_retired_manifest("pr_comment_authorization_continuation_registration")
    )
    _arm_old_issue_marker(fake)
    publisher = _continuation_publisher(fake)
    _assert_retired_registration(fake, publisher=publisher)
    _assert_retired_registration(fake, publisher=publisher)
    assert _post_count(fake, PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER) == 0


def test_continuation_requires_protected_tip_to_match_source_run_sha() -> None:
    fake = FakeTransport(manifest=_protected_pause_manifest())
    fake.protected_tip = "b" * 40

    with pytest.raises(
        PublisherError,
        match="protected develop tip does not match source run SHA",
    ):
        _publisher(fake).reconcile_diagnostic_or_normal(
            SOURCE_RUN,
            1,
            event_name="workflow_run",
        )

    _assert_zero_mutation(fake)


def test_continuation_missing_pause_is_pending_and_does_not_post_partial_state() -> None:
    fake = FakeTransport(manifest="schema_version: 6\n")
    _assert_pending_authorization(fake, event_name="workflow_run")


def test_continuation_failing_producer_has_zero_diagnostic_posts() -> None:
    fake = FakeTransport(manifest=_protected_pause_manifest())
    fake.producer_pass = False
    _assert_pending_authorization(fake, event_name="workflow_run")
    assert fake.comments[PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER] == []


def test_continuation_run_6_is_authorization_blocked_without_finalizer_lookup() -> None:
    fake = FakeTransport(
        manifest=_retired_manifest("pr_comment_authorization_continuation_registration")
    )
    fake.finalizer_run_number = 6
    _arm_old_issue_marker(fake)
    _assert_retired_registration(fake, publisher=_continuation_publisher(fake))
    assert not any(
        "/actions/workflows/373741433/runs" in path
        for _, path, _, _ in fake.calls
    )


def test_continuation_registration_wrong_current_source_pr_is_zero_post() -> None:
    fake = FakeTransport(
        manifest=_retired_manifest(
            "pr_comment_authorization_continuation_registration", pr_number=999
        )
    )
    _arm_old_issue_marker(fake)
    _assert_retired_registration(fake, publisher=_continuation_publisher(fake))


def test_continuation_locked_target_is_zero_post() -> None:
    fake = FakeTransport(
        manifest=_retired_manifest("pr_comment_authorization_continuation_registration")
    )
    fake.pr13_locked = True
    _arm_old_issue_marker(fake)
    _assert_retired_registration(fake, publisher=_continuation_publisher(fake))


def test_continuation_forged_duplicate_marker_is_zero_post() -> None:
    fake = FakeTransport(
        manifest=_retired_manifest("pr_comment_authorization_continuation_registration")
    )
    _arm_old_issue_marker(fake)
    fake.comments[PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER] = [
        {
            "id": 1,
            "body": f"{PR_COMMENT_AUTHORIZATION_CONTINUATION_MARKER}\n```json\n{{}}\n```",
            "user": {"login": "untrusted", "type": "User"},
        }
    ]

    _assert_retired_registration(fake, publisher=_continuation_publisher(fake))


def test_continuation_403_preserves_sanitized_evidence_and_never_retries() -> None:
    fake = FakeTransport(
        manifest=_retired_manifest("pr_comment_authorization_continuation_registration")
    )
    _arm_old_issue_marker(fake)
    fake.post_errors[PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER] = ApiError(
        "POST",
        f"{REPOSITORY_PATH}/issues/13/comments",
        403,
        "status=403; message=Resource not accessible by integration; "
        "X-GitHub-Request-Id=abcd:123456:abcdef:7890ab:12345678; "
        "X-Accepted-GitHub-Permissions=issues=write, pull-requests=write; "
        "Authorization=COMMENT_TOKEN_SENTINEL; body=COMMENT_BODY_SENTINEL",
    )

    with pytest.raises(PublisherError, match="retired") as raised:
        _continuation_publisher(fake).reconcile_diagnostic_or_normal(
            SOURCE_RUN,
            1,
            event_name="workflow_run",
        )
    message = str(raised.value)
    assert "COMMENT_TOKEN_SENTINEL" not in message
    assert "COMMENT_BODY_SENTINEL" not in message
    _assert_zero_mutation(fake)


def test_continuation_missing_readback_stops_after_one_post() -> None:
    fake = FakeTransport(
        manifest=_retired_manifest("pr_comment_authorization_continuation_registration")
    )
    _arm_old_issue_marker(fake)
    fake.hide_pr13_readback = True
    _assert_retired_registration(fake, publisher=_continuation_publisher(fake))


def test_continuation_uncertain_post_reads_back_once_without_reposting() -> None:
    fake = FakeTransport(
        manifest=_retired_manifest("pr_comment_authorization_continuation_registration")
    )
    _arm_old_issue_marker(fake)
    fake.uncertain_post_without_write.add(PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER)
    _assert_retired_registration(fake, publisher=_continuation_publisher(fake))
    assert fake.comments[PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER] == []


def test_continuation_old_marker_wrong_identity_is_zero_post() -> None:
    fake = FakeTransport(
        manifest=_retired_manifest("pr_comment_authorization_continuation_registration")
    )
    _arm_old_issue_marker(fake)
    fake.comments[PAIRED_COMMENT_DIAGNOSTIC_ISSUE_NUMBER][0]["id"] = OLD_PAIRED_MARKER_COMMENT_ID + 1
    _assert_retired_registration(fake, publisher=_continuation_publisher(fake))


def test_continuation_wrong_event_is_pending_and_zero_mutation() -> None:
    fake = FakeTransport(manifest=_protected_pause_manifest())
    _arm_old_issue_marker(fake)
    publisher = _continuation_publisher(fake)
    normal_calls: list[tuple[int | None, int | None]] = []

    def normal(run_id: int | None, attempt: int | None) -> list[Any]:
        normal_calls.append((run_id, attempt))
        return []

    publisher.reconcile_all = normal  # type: ignore[method-assign]
    _assert_pending_authorization(
        fake,
        publisher=publisher,
        event_name="workflow_dispatch",
    )
    assert normal_calls == []


def test_active_registration_is_retired_without_comment_mutation() -> None:
    fake = FakeTransport(manifest=_retired_manifest("paired_comment_diagnostic_registration"))
    _assert_retired_registration(fake)
    assert fake.comments[17] == []
    assert fake.comments[13] == []


def test_matching_registration_with_failing_producer_remains_blocked() -> None:
    fake = FakeTransport(manifest=_retired_manifest("paired_comment_diagnostic_registration"))
    fake.producer_pass = False
    publisher = _publisher(fake)
    normal_calls: list[tuple[int | None, int | None]] = []

    def normal(run_id: int | None, attempt: int | None) -> list[Any]:
        normal_calls.append((run_id, attempt))
        return []

    publisher.reconcile_all = normal  # type: ignore[method-assign]
    _assert_retired_registration(fake, publisher=publisher)
    assert normal_calls == []
    assert fake.comments[17] == []
    assert fake.comments[13] == []


def test_rerun_of_old_marker_stays_pending_without_reposting() -> None:
    fake = FakeTransport(manifest=_protected_pause_manifest())
    _arm_old_issue_marker(fake)
    publisher = _publisher(fake)
    _assert_pending_authorization(fake, publisher=publisher, event_name="workflow_run")
    _assert_pending_authorization(fake, publisher=publisher, event_name="workflow_run")
    assert fake.comments[17]
    assert fake.comments[13] == []


def test_uncertain_post_is_not_attempted_after_retirement() -> None:
    fake = FakeTransport(manifest=_protected_pause_manifest())
    fake.uncertain_post_with_write.add(17)
    _assert_pending_authorization(fake, event_name="workflow_run")
    assert fake.comments[17] == []
    assert fake.comments[13] == []


def test_partial_pair_fences_rerun_after_pr_forbidden_without_reposting() -> None:
    fake = FakeTransport(manifest=_protected_pause_manifest())
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

    with pytest.raises(PublisherError, match="pending-authorization") as first:
        publisher.reconcile_diagnostic_or_normal(
            SOURCE_RUN,
            1,
            event_name="workflow_run",
        )
    first_message = str(first.value)
    assert "COMMENT_TOKEN_SENTINEL" not in first_message
    assert "COMMENT_BODY_SENTINEL" not in first_message
    _assert_zero_mutation(fake)

    with pytest.raises(PublisherError, match="pending-authorization"):
        publisher.reconcile_diagnostic_or_normal(
            SOURCE_RUN,
            1,
            event_name="workflow_run",
        )
    _assert_zero_mutation(fake)


def test_uncertain_post_without_readback_stops_without_posting_second_target() -> None:
    fake = FakeTransport(manifest=_protected_pause_manifest())
    fake.uncertain_post_without_write.add(17)
    _assert_pending_authorization(fake, event_name="workflow_run")
    assert fake.comments[17] == []
    assert fake.comments[13] == []


def test_diagnostic_manual_context_is_pending_without_normal_fallback() -> None:
    fake = FakeTransport(manifest=_protected_pause_manifest())
    publisher = _publisher(fake)
    called: list[tuple[int | None, int | None]] = []

    def normal(run_id: int | None, attempt: int | None) -> list[Any]:
        called.append((run_id, attempt))
        return []

    publisher.reconcile_all = normal  # type: ignore[method-assign]
    _assert_pending_authorization(
        fake,
        publisher=publisher,
        event_name="workflow_dispatch",
    )
    assert called == []
    assert fake.comments[17] == []
    assert fake.comments[13] == []


def test_missing_pause_fails_closed_without_normal_reconciliation() -> None:
    fake = FakeTransport(manifest="schema_version: 6\n")
    publisher = _publisher(fake)
    called: list[tuple[int | None, int | None]] = []

    def normal(run_id: int | None, attempt: int | None) -> list[Any]:
        called.append((run_id, attempt))
        return []

    publisher.reconcile_all = normal  # type: ignore[method-assign]
    _assert_pending_authorization(
        fake,
        publisher=publisher,
        event_name="workflow_run",
    )
    assert called == []
    assert fake.comments[17] == []
    assert fake.comments[13] == []


def test_finalizer_uses_read_only_pull_request_permission_after_cleanup() -> None:
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
    assert jobs.count("pull-requests: read") == 1
    assert "matrix:" not in jobs
    assert jobs.count("python tools/post_merge_publisher.py --diagnostic-aware") == 1
    assert "secrets.PAT" not in workflow
    assert "GCM" not in workflow
    assert "continue-on-error" not in workflow
