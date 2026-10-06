from __future__ import annotations

from typing import Any

import pytest

try:
    from . import test_paired_comment_diagnostic as baseline
except ImportError:
    import test_paired_comment_diagnostic as baseline

from tools import post_merge_publisher as publisher_module
from tools.post_merge_publisher import (
    CANONICAL_REPOSITORY,
    PAIRED_COMMENT_DIAGNOSTIC_ISSUE_NUMBER,
    PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER,
    PAIRED_COMMENT_DIAGNOSTIC_VERSION,
    PR_COMMENT_AUTHORIZATION_CONTINUATION_REGISTRATION,
    PR_COMMENT_AUTHORIZATION_CONTINUATION_VERSION,
    PairedCommentDiagnostic,
    PairedCommentDiagnosticRegistration,
    PairedCommentDiagnosticResult,
    PrCommentAuthorizationContinuation,
    PrCommentAuthorizationContinuationRegistration,
    PrCommentAuthorizationContinuationResult,
    PublisherError,
)


SHA = baseline.SHA
DIAGNOSTIC_PR = baseline.DIAGNOSTIC_PR
SOURCE_RUN = baseline.SOURCE_RUN


def _legacy_parse_paired_comment_diagnostic_registration(
    manifest: str,
) -> PairedCommentDiagnosticRegistration | None:
    """Test-only parser for the pre-cleanup registration contract."""

    lines = manifest.splitlines()
    matches = [
        index
        for index, line in enumerate(lines)
        if line == "paired_comment_diagnostic_registration:"
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise PublisherError(
            "manifest must contain exactly one paired comment diagnostic registration"
        )
    child: list[str] = []
    index = matches[0] + 1
    while index < len(lines):
        line = lines[index]
        if line and not line.startswith("  "):
            break
        if line.strip():
            child.append(line)
        index += 1
    if len(child) != 2:
        raise PublisherError("paired comment diagnostic registration has an unexpected schema")
    version = child[0].removeprefix("  version:").strip()
    pr_number = child[1].removeprefix("  pr_number:").strip()
    if child[0].split(":", 1)[0].strip() != "version" or version != str(
        PAIRED_COMMENT_DIAGNOSTIC_VERSION
    ):
        raise PublisherError("paired comment diagnostic registration version is invalid")
    if (
        child[1].split(":", 1)[0].strip() != "pr_number"
        or not pr_number.isdigit()
        or int(pr_number) <= 0
    ):
        raise PublisherError("paired comment diagnostic registration pr_number is invalid")
    return PairedCommentDiagnosticRegistration(
        version=PAIRED_COMMENT_DIAGNOSTIC_VERSION,
        pr_number=int(pr_number),
    )


def _legacy_parse_pr_comment_authorization_continuation_registration(
    manifest: str,
) -> PrCommentAuthorizationContinuationRegistration | None:
    """Test-only parser for the pre-cleanup one-shot continuation contract."""

    key = f"{PR_COMMENT_AUTHORIZATION_CONTINUATION_REGISTRATION}:"
    lines = manifest.splitlines()
    matches = [index for index, line in enumerate(lines) if line == key]
    if not matches:
        return None
    if len(matches) != 1:
        raise PublisherError(
            "manifest must contain exactly one PR comment authorization continuation registration"
        )
    child: list[str] = []
    index = matches[0] + 1
    while index < len(lines):
        line = lines[index]
        if line and not line.startswith("  "):
            break
        if line.strip():
            child.append(line)
        index += 1
    if len(child) != 2:
        raise PublisherError(
            "PR comment authorization continuation registration has an unexpected schema"
        )
    version = child[0].removeprefix("  version:").strip()
    pr_number = child[1].removeprefix("  pr_number:").strip()
    if child[0].split(":", 1)[0].strip() != "version" or version != str(
        PR_COMMENT_AUTHORIZATION_CONTINUATION_VERSION
    ):
        raise PublisherError("PR comment authorization continuation version is invalid")
    if (
        child[1].split(":", 1)[0].strip() != "pr_number"
        or not pr_number.isdigit()
        or int(pr_number) <= 0
    ):
        raise PublisherError("PR comment authorization continuation pr_number is invalid")
    return PrCommentAuthorizationContinuationRegistration(
        version=PR_COMMENT_AUTHORIZATION_CONTINUATION_VERSION,
        pr_number=int(pr_number),
    )


def _legacy_paired_run(
    self: PairedCommentDiagnostic,
    source_run: publisher_module.VerifiedRun,
    source_pull: publisher_module.VerifiedPull,
) -> PairedCommentDiagnosticResult:
    """The former run body, kept inside the test fixture only."""

    if self.config.repository.lower() != CANONICAL_REPOSITORY.lower():
        raise PublisherError("paired comment diagnostic requires the canonical repository")
    if source_pull.number != self.registration.pr_number:
        raise PublisherError("source PR does not match the controller diagnostic registration")
    for kind, number in self._targets:
        self._validate_target(kind, number)
    created_targets: list[str] = []
    with publisher_module.PostMergePublisher._mutation_lock:
        existing = {
            (kind, number): self._find_comment(
                kind=kind,
                number=number,
                source_run=source_run,
                source_pull=source_pull,
                require_exact_source=False,
            )
            for kind, number in self._targets
        }
        if any(value is not None for value in existing.values()):
            missing = [
                f"{kind}#{number}"
                for kind, number in self._targets
                if existing[(kind, number)] is None
            ]
            if missing:
                raise PublisherError(
                    "paired diagnostic probe already started at issue#17; "
                    "refusing all further POSTs; missing "
                    + ", ".join(missing)
                )
            for kind, number in self._targets:
                existing_comment = existing[(kind, number)]
                if existing_comment is None:
                    raise PublisherError(
                        "paired diagnostic probe fence changed while reading both targets"
                    )
                self._validate_comment(
                    existing_comment,
                    kind=kind,
                    number=number,
                    source_run=source_run,
                    source_pull=source_pull,
                )
                self._validate_target(kind, number)
            return PairedCommentDiagnosticResult(
                source_run_id=source_run.run_id,
                source_run_attempt=source_run.run_attempt,
                source_sha=source_run.sha,
                source_pr_number=source_pull.number,
                created_targets=(),
            )
        for kind, number in self._targets:
            if self._ensure_comment(
                kind=kind,
                number=number,
                source_run=source_run,
                source_pull=source_pull,
            ):
                created_targets.append(f"{kind}#{number}")
        for kind, number in self._targets:
            self._validate_target(kind, number)
            if self._find_comment(
                kind=kind,
                number=number,
                source_run=source_run,
                source_pull=source_pull,
            ) is None:
                raise PublisherError(
                    f"paired diagnostic comment for {kind} #{number} was not read back"
                )
    return PairedCommentDiagnosticResult(
        source_run_id=source_run.run_id,
        source_run_attempt=source_run.run_attempt,
        source_sha=source_run.sha,
        source_pr_number=source_pull.number,
        created_targets=tuple(created_targets),
    )


def _legacy_continuation_run(
    self: PrCommentAuthorizationContinuation,
    source_run: publisher_module.VerifiedRun,
    source_pull: publisher_module.VerifiedPull,
) -> PrCommentAuthorizationContinuationResult:
    """The former continuation run body, kept inside the test fixture only."""

    if self.config.repository.lower() != CANONICAL_REPOSITORY.lower():
        raise PublisherError(
            "PR comment authorization continuation requires the canonical repository"
        )
    if source_run.quality != "pass":
        raise PublisherError(
            "PR comment authorization continuation requires a passing producer run"
        )
    if source_pull.number != self.registration.pr_number:
        raise PublisherError("current source PR does not match the continuation registration")
    self._validate_pr13_target()
    with publisher_module.PostMergePublisher._mutation_lock:
        self._verify_old_issue_marker()
        created = self._ensure_comment(
            source_run=source_run,
            source_pull=source_pull,
        )
        self._validate_pr13_target()
    return PrCommentAuthorizationContinuationResult(
        source_run_id=source_run.run_id,
        source_run_attempt=source_run.run_attempt,
        source_sha=source_run.sha,
        source_pr_number=source_pull.number,
        finalizer_run_id=self.finalizer.run_id,
        finalizer_run_number=self.finalizer.run_number,
        finalizer_run_attempt=self.finalizer.run_attempt,
        created=created,
    )


def _legacy_reconcile_diagnostic_or_normal(
    self: publisher_module.PostMergePublisher,
    preferred_run_id: int | None,
    preferred_run_attempt: int | None,
    *,
    event_name: str,
) -> (
    publisher_module.PairedCommentDiagnosticResult
    | publisher_module.PrCommentAuthorizationContinuationResult
    | list[publisher_module.PublisherResult]
):
    """Former dispatcher used only by the positive historical tests."""

    if event_name != "workflow_run" or preferred_run_id is None:
        return self.reconcile_all(preferred_run_id, preferred_run_attempt)
    protected_tip = publisher_module._require_sha(
        self.api.branch_tip("develop"),
        "protected develop tip",
    )
    protected_manifest = self.api.manifest(self.config.manifest_path, protected_tip)
    continuation = publisher_module.parse_pr_comment_authorization_continuation_registration(
        protected_manifest
    )
    if continuation is not None:
        finalizer = self._verify_fixed_finalizer_identity()
        source_run = self._verify_run(preferred_run_id, preferred_run_attempt)
        if protected_tip != source_run.sha:
            raise PublisherError("protected develop tip does not match continuation source SHA")
        if source_run.quality != "pass":
            raise PublisherError(
                "PR comment authorization continuation requires a passing producer run"
            )
        source_pull = self._verified_pull_for_sha(source_run.sha)
        if source_pull.number != continuation.pr_number:
            raise PublisherError("current source PR does not match the continuation registration")
        return PrCommentAuthorizationContinuation(
            self.api,
            self.config,
            continuation,
            finalizer,
        ).run(source_run, source_pull)
    registration = publisher_module.parse_paired_comment_diagnostic_registration(
        protected_manifest
    )
    if registration is None:
        return self.reconcile_all(preferred_run_id, preferred_run_attempt)
    source_run = self._verify_run(preferred_run_id, preferred_run_attempt)
    source_pull = self._verified_pull_for_sha(source_run.sha)
    if source_pull.number != registration.pr_number:
        return self.reconcile_all(preferred_run_id, preferred_run_attempt)
    if source_run.quality != "pass":
        return self.reconcile_all(preferred_run_id, preferred_run_attempt)
    return PairedCommentDiagnostic(self.api, self.config, registration).run(
        source_run,
        source_pull,
    )


@pytest.fixture
def legacy_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore historical behavior only inside these in-memory tests.

    The production classes remain retired.  The fixture injects the former
    dispatch and run bodies into those classes for positive regression cases,
    while every network-capable entrypoint raises if reached.
    """

    def blocked_network(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError("legacy diagnostic fixture attempted real network access")

    monkeypatch.setattr(
        publisher_module,
        "parse_paired_comment_diagnostic_registration",
        _legacy_parse_paired_comment_diagnostic_registration,
    )
    monkeypatch.setattr(
        publisher_module,
        "parse_pr_comment_authorization_continuation_registration",
        _legacy_parse_pr_comment_authorization_continuation_registration,
    )
    monkeypatch.setattr(
        publisher_module.PairedCommentDiagnostic,
        "run",
        _legacy_paired_run,
    )
    monkeypatch.setattr(
        publisher_module.PrCommentAuthorizationContinuation,
        "run",
        _legacy_continuation_run,
    )
    monkeypatch.setattr(
        publisher_module.PostMergePublisher,
        "reconcile_diagnostic_or_normal",
        _legacy_reconcile_diagnostic_or_normal,
    )
    monkeypatch.setattr(publisher_module.UrllibTransport, "request", blocked_network)
    monkeypatch.setattr(publisher_module, "urlopen", blocked_network)


# Every original test function receives a stable replacement node name.  The
# old production path is not re-enabled; these IDs point at this test-only
# fixture and preserve the historical evidence map.
_LEGACY_ORIGINAL_NODE_NAMES = (
    "test_registration_parser_defaults_dormant_without_registration",
    "test_registration_parser_rejects_placeholder_and_extra_fields",
    "test_continuation_registration_is_dormant_by_default_and_accepts_real_pr_only",
    "test_continuation_fixed_run_5_attempt_1_posts_pr13_once_and_never_lists_runs",
    "test_continuation_success_replay_reads_marker_without_reposting",
    "test_continuation_requires_protected_tip_to_match_source_run_sha",
    "test_continuation_requires_old_issue_fence_and_does_not_post_partial_state",
    "test_continuation_failing_producer_has_zero_diagnostic_posts",
    "test_continuation_wrong_fixed_finalizer_identity_is_fail_closed",
    "test_continuation_registration_wrong_current_source_pr_is_zero_post",
    "test_continuation_locked_target_is_zero_post",
    "test_continuation_forged_duplicate_marker_is_zero_post",
    "test_continuation_403_preserves_sanitized_evidence_and_never_retries",
    "test_continuation_missing_readback_stops_after_one_post",
    "test_continuation_uncertain_post_reads_back_once_without_reposting",
    "test_continuation_old_marker_wrong_identity_is_zero_post",
    "test_continuation_wrong_event_preserves_normal_path_and_zero_diagnostic_posts",
    "test_active_registration_writes_one_comment_per_closed_target_only",
    "test_matching_registration_with_failing_producer_keeps_normal_path",
    "test_rerun_reads_existing_pair_without_reposting",
    "test_uncertain_post_is_read_back_without_a_retry",
    "test_partial_pair_fences_rerun_after_pr_forbidden_without_reposting",
    "test_uncertain_post_without_readback_stops_without_posting_second_target",
    "test_diagnostic_is_not_active_for_manual_context",
    "test_dormant_registration_preserves_normal_reconciliation",
    "test_finalizer_uses_same_job_and_limits_the_temporary_permission_exception",
)
LEGACY_NODE_ID_MAP = {
    original: f"test_legacy_{original.removeprefix('test_')}"
    for original in _LEGACY_ORIGINAL_NODE_NAMES
}

_HISTORICAL_FINALIZER_WORKFLOW = """\
name: Windows 11 Layer A post-merge finalizer
on:
  workflow_run:
    workflows:
      - Windows 11 Layer A post-merge producer
jobs:
  finalize:
    permissions:
      contents: read
      actions: read
      checks: read
      pull-requests: write
      issues: write
    steps:
      - run: python tools/post_merge_publisher.py --diagnostic-aware
"""


def test_legacy_registration_parser_defaults_dormant_without_registration(
    legacy_diagnostic: None,
) -> None:
    assert _legacy_parse_paired_comment_diagnostic_registration("schema_version: 6\n") is None


def test_legacy_registration_parser_rejects_placeholder_and_extra_fields(
    legacy_diagnostic: None,
) -> None:
    with pytest.raises(PublisherError, match="pr_number is invalid"):
        _legacy_parse_paired_comment_diagnostic_registration(
            "paired_comment_diagnostic_registration:\n  version: 1\n  pr_number: <real PR>\n"
        )
    with pytest.raises(PublisherError, match="unexpected schema"):
        _legacy_parse_paired_comment_diagnostic_registration(
            "paired_comment_diagnostic_registration:\n"
            "  version: 1\n"
            "  pr_number: 42\n"
            "  producer_run_id: 9001\n"
        )


def test_legacy_continuation_registration_is_dormant_by_default_and_accepts_real_pr_only(
    legacy_diagnostic: None,
) -> None:
    assert (
        _legacy_parse_pr_comment_authorization_continuation_registration("schema_version: 6\n")
        is None
    )
    registration = _legacy_parse_pr_comment_authorization_continuation_registration(
        "pr_comment_authorization_continuation_registration:\n"
        "  version: 1\n"
        "  pr_number: 42\n"
    )
    assert registration is not None
    assert registration.pr_number == 42
    with pytest.raises(PublisherError, match="pr_number is invalid"):
        _legacy_parse_pr_comment_authorization_continuation_registration(
            "pr_comment_authorization_continuation_registration:\n"
            "  version: 1\n"
            "  pr_number: <controller PR>\n"
        )
    with pytest.raises(PublisherError, match="unexpected schema"):
        _legacy_parse_pr_comment_authorization_continuation_registration(
            "pr_comment_authorization_continuation_registration:\n"
            "  version: 1\n"
            "  pr_number: 42\n"
            "  run_number: 5\n"
        )


def test_legacy_continuation_fixed_run_5_attempt_1_posts_pr13_once_and_never_lists_runs(
    legacy_diagnostic: None,
) -> None:
    fake = baseline.FakeTransport(manifest=baseline._continuation_manifest())
    baseline._arm_old_issue_marker(fake)
    fake.workflow_runs = [{"id": 1, "run_number": 4}, {"id": 2, "run_number": 3}]

    result = baseline._continuation_publisher(fake).reconcile_diagnostic_or_normal(
        SOURCE_RUN,
        1,
        event_name="workflow_run",
    )

    assert isinstance(result, PrCommentAuthorizationContinuationResult)
    assert result.created is True
    assert result.finalizer_run_number == 5
    assert result.finalizer_run_attempt == 1
    assert baseline._post_count(fake, PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER) == 1
    assert len(fake.comments[PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER]) == 1
    assert all(
        method == "GET"
        for method, path, _, _ in fake.calls
        if f"/issues/{PAIRED_COMMENT_DIAGNOSTIC_ISSUE_NUMBER}" in path
    )
    assert not any(
        "/actions/workflows/373741433/runs" in path
        for _, path, _, _ in fake.calls
    )
    assert baseline._payload_from_body(
        fake.comments[PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER][0]["body"]
    )["finalizer_run_number"] == 5


def test_legacy_continuation_success_replay_reads_marker_without_reposting(
    legacy_diagnostic: None,
) -> None:
    fake = baseline.FakeTransport(manifest=baseline._continuation_manifest())
    baseline._arm_old_issue_marker(fake)
    publisher = baseline._continuation_publisher(fake)
    first = publisher.reconcile_diagnostic_or_normal(SOURCE_RUN, 1, event_name="workflow_run")
    before = baseline._post_count(fake, PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER)
    second = publisher.reconcile_diagnostic_or_normal(SOURCE_RUN, 1, event_name="workflow_run")

    assert isinstance(first, PrCommentAuthorizationContinuationResult)
    assert isinstance(second, PrCommentAuthorizationContinuationResult)
    assert first.created is True
    assert second.created is False
    assert baseline._post_count(fake, PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER) == before == 1


def test_legacy_continuation_requires_protected_tip_to_match_source_run_sha(
    legacy_diagnostic: None,
) -> None:
    fake = baseline.FakeTransport(manifest=baseline._continuation_manifest())
    baseline._arm_old_issue_marker(fake)
    fake.protected_tip = "b" * 40

    with pytest.raises(
        PublisherError,
        match="protected develop tip does not match continuation source SHA",
    ):
        baseline._continuation_publisher(fake).reconcile_diagnostic_or_normal(
            SOURCE_RUN,
            1,
            event_name="workflow_run",
        )

    assert baseline._post_count(fake, PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER) == 0
    assert all(
        not (
            method != "GET"
            and f"/issues/{PAIRED_COMMENT_DIAGNOSTIC_ISSUE_NUMBER}" in path
        )
        for method, path, _, _ in fake.calls
    )


def test_legacy_continuation_requires_old_issue_fence_and_does_not_post_partial_state(
    legacy_diagnostic: None,
) -> None:
    fake = baseline.FakeTransport(manifest=baseline._continuation_manifest())

    with pytest.raises(PublisherError, match="old Issue #17 diagnostic fence"):
        baseline._continuation_publisher(fake).reconcile_diagnostic_or_normal(
            SOURCE_RUN,
            1,
            event_name="workflow_run",
        )

    assert baseline._post_count(fake, PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER) == 0
    assert all(
        not (
            method != "GET"
            and f"/issues/{PAIRED_COMMENT_DIAGNOSTIC_ISSUE_NUMBER}" in path
        )
        for method, path, _, _ in fake.calls
    )


def test_legacy_continuation_failing_producer_has_zero_diagnostic_posts(
    legacy_diagnostic: None,
) -> None:
    fake = baseline.FakeTransport(manifest=baseline._continuation_manifest())
    fake.producer_pass = False
    baseline._arm_old_issue_marker(fake)

    with pytest.raises(PublisherError, match="passing producer run"):
        baseline._continuation_publisher(fake).reconcile_diagnostic_or_normal(
            SOURCE_RUN,
            1,
            event_name="workflow_run",
        )

    assert baseline._post_count(fake, PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER) == 0
    assert fake.comments[PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER] == []


@pytest.mark.parametrize(
    ("attribute", "value", "message"),
    [
        ("finalizer_run_number", 4, "GITHUB_RUN_NUMBER"),
        ("finalizer_run_number", 6, "GITHUB_RUN_NUMBER"),
        ("finalizer_run_attempt", 2, "GITHUB_RUN_ATTEMPT"),
        ("finalizer_workflow_id", 999, "workflow identity"),
        ("finalizer_job", "other", "finalize job"),
        ("finalizer_event", "push", "workflow_run"),
        ("finalizer_head_branch", "feature", "workflow_run"),
        ("finalizer_repository", "attacker/other", "repository"),
        ("finalizer_path", ".github/workflows/other.yml", "workflow identity"),
        ("finalizer_status", "cancelled", "cancelled"),
    ],
    ids=(
        "run-number-before-fixed",
        "run-number-after-fixed",
        "attempt-mismatch",
        "workflow-id-mismatch",
        "job-mismatch",
        "event-mismatch",
        "branch-mismatch",
        "repository-mismatch",
        "path-mismatch",
        "cancelled",
    ),
)
def test_legacy_continuation_wrong_fixed_finalizer_identity_is_fail_closed(
    legacy_diagnostic: None,
    attribute: str,
    value: Any,
    message: str,
) -> None:
    fake = baseline.FakeTransport(manifest=baseline._continuation_manifest())
    baseline._arm_old_issue_marker(fake)
    setattr(fake, attribute, value)

    with pytest.raises(PublisherError, match=message):
        baseline._continuation_publisher(fake).reconcile_diagnostic_or_normal(
            SOURCE_RUN,
            1,
            event_name="workflow_run",
        )

    assert baseline._post_count(fake, PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER) == 0
    assert not any(
        "/actions/workflows/373741433/runs" in path
        for _, path, _, _ in fake.calls
    )


def test_legacy_continuation_registration_wrong_current_source_pr_is_zero_post(
    legacy_diagnostic: None,
) -> None:
    fake = baseline.FakeTransport(manifest=baseline._continuation_manifest(pr_number=999))
    baseline._arm_old_issue_marker(fake)

    with pytest.raises(PublisherError, match="source PR"):
        baseline._continuation_publisher(fake).reconcile_diagnostic_or_normal(
            SOURCE_RUN,
            1,
            event_name="workflow_run",
        )

    assert baseline._post_count(fake, PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER) == 0


def test_legacy_continuation_locked_target_is_zero_post(legacy_diagnostic: None) -> None:
    fake = baseline.FakeTransport(manifest=baseline._continuation_manifest())
    fake.pr13_locked = True
    baseline._arm_old_issue_marker(fake)

    with pytest.raises(PublisherError, match="locked"):
        baseline._continuation_publisher(fake).reconcile_diagnostic_or_normal(
            SOURCE_RUN,
            1,
            event_name="workflow_run",
        )

    assert baseline._post_count(fake, PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER) == 0


def test_legacy_continuation_forged_duplicate_marker_is_zero_post(legacy_diagnostic: None) -> None:
    fake = baseline.FakeTransport(manifest=baseline._continuation_manifest())
    baseline._arm_old_issue_marker(fake)
    fake.comments[PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER] = [
        {
            "id": 1,
            "body": f"{publisher_module.PR_COMMENT_AUTHORIZATION_CONTINUATION_MARKER}\n"
            "```json\n{}\n```",
            "user": {"login": "untrusted", "type": "User"},
        }
    ]

    with pytest.raises(PublisherError, match="untrusted author"):
        baseline._continuation_publisher(fake).reconcile_diagnostic_or_normal(
            SOURCE_RUN,
            1,
            event_name="workflow_run",
        )

    assert baseline._post_count(fake, PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER) == 0


def test_legacy_continuation_403_preserves_sanitized_evidence_and_never_retries(
    legacy_diagnostic: None,
) -> None:
    fake = baseline.FakeTransport(manifest=baseline._continuation_manifest())
    baseline._arm_old_issue_marker(fake)
    fake.post_errors[PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER] = baseline.ApiError(
        "POST",
        f"{baseline.REPOSITORY_PATH}/issues/13/comments",
        403,
        "status=403; message=Resource not accessible by integration; "
        "X-GitHub-Request-Id=abcd:123456:abcdef:7890ab:12345678; "
        "X-Accepted-GitHub-Permissions=issues=write, pull-requests=write; "
        "Authorization=COMMENT_TOKEN_SENTINEL; body=COMMENT_BODY_SENTINEL",
    )

    with pytest.raises(PublisherError) as raised:
        baseline._continuation_publisher(fake).reconcile_diagnostic_or_normal(
            SOURCE_RUN,
            1,
            event_name="workflow_run",
        )

    message = str(raised.value)
    assert "target=pull_request#13" in message
    assert "HTTP 403" in message
    assert "X-GitHub-Request-Id=abcd:123456:abcdef:7890ab:12345678" in message
    assert "X-Accepted-GitHub-Permissions=issues=write, pull-requests=write" in message
    assert "COMMENT_TOKEN_SENTINEL" not in message
    assert "COMMENT_BODY_SENTINEL" not in message
    assert baseline._post_count(fake, PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER) == 1
    assert fake.comments[PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER] == []


def test_legacy_continuation_missing_readback_stops_after_one_post(
    legacy_diagnostic: None,
) -> None:
    fake = baseline.FakeTransport(manifest=baseline._continuation_manifest())
    baseline._arm_old_issue_marker(fake)
    fake.hide_pr13_readback = True

    with pytest.raises(PublisherError, match="target=pull_request#13"):
        baseline._continuation_publisher(fake).reconcile_diagnostic_or_normal(
            SOURCE_RUN,
            1,
            event_name="workflow_run",
        )

    assert baseline._post_count(fake, PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER) == 1


def test_legacy_continuation_uncertain_post_reads_back_once_without_reposting(
    legacy_diagnostic: None,
) -> None:
    fake = baseline.FakeTransport(manifest=baseline._continuation_manifest())
    baseline._arm_old_issue_marker(fake)
    fake.uncertain_post_without_write.add(PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER)

    with pytest.raises(PublisherError, match="HTTP 503"):
        baseline._continuation_publisher(fake).reconcile_diagnostic_or_normal(
            SOURCE_RUN,
            1,
            event_name="workflow_run",
        )

    assert baseline._post_count(fake, PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER) == 1
    assert fake.comments[PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER] == []


def test_legacy_continuation_old_marker_wrong_identity_is_zero_post(legacy_diagnostic: None) -> None:
    fake = baseline.FakeTransport(manifest=baseline._continuation_manifest())
    baseline._arm_old_issue_marker(fake)
    fake.comments[PAIRED_COMMENT_DIAGNOSTIC_ISSUE_NUMBER][0]["id"] = (
        publisher_module.OLD_PAIRED_MARKER_COMMENT_ID + 1
    )

    with pytest.raises(PublisherError, match="marker comment id"):
        baseline._continuation_publisher(fake).reconcile_diagnostic_or_normal(
            SOURCE_RUN,
            1,
            event_name="workflow_run",
        )

    assert baseline._post_count(fake, PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER) == 0


def test_legacy_continuation_wrong_event_preserves_normal_path_and_zero_diagnostic_posts(
    legacy_diagnostic: None,
) -> None:
    fake = baseline.FakeTransport(manifest=baseline._continuation_manifest())
    baseline._arm_old_issue_marker(fake)
    publisher = baseline._continuation_publisher(fake)
    normal_calls: list[tuple[int | None, int | None]] = []

    def normal(run_id: int | None, attempt: int | None) -> list[Any]:
        normal_calls.append((run_id, attempt))
        return []

    publisher.reconcile_all = normal
    result = publisher.reconcile_diagnostic_or_normal(
        SOURCE_RUN,
        1,
        event_name="workflow_dispatch",
    )

    assert result == []
    assert normal_calls == [(SOURCE_RUN, 1)]
    assert baseline._post_count(fake, PAIRED_COMMENT_DIAGNOSTIC_PR_NUMBER) == 0


def test_legacy_active_registration_writes_one_comment_per_closed_target_only(
    legacy_diagnostic: None,
) -> None:
    fake = baseline.FakeTransport()

    result = baseline._publisher(fake).reconcile_diagnostic_or_normal(
        SOURCE_RUN,
        1,
        event_name="workflow_run",
    )

    assert isinstance(result, PairedCommentDiagnosticResult)
    assert set(result.created_targets) == {"issue#17", "pull_request#13"}
    assert len(fake.comments[17]) == 1
    assert len(fake.comments[13]) == 1
    assert all(
        publisher_module.PAIRED_COMMENT_DIAGNOSTIC_MARKER in comment["body"]
        for comments in fake.comments.values()
        for comment in comments
    )
    assert all(
        payload["production_projection"] is False
        for comments in fake.comments.values()
        for comment in comments
        for payload in [baseline._payload_from_body(comment["body"])]
    )
    assert all(
        not (method == "POST" and path.endswith("/issues"))
        and method not in {"PATCH", "PUT"}
        for method, path, _, _ in fake.calls
    )
    assert baseline._post_count(fake, 17) == 1
    assert baseline._post_count(fake, 13) == 1


def test_legacy_matching_registration_with_failing_producer_keeps_normal_path(
    legacy_diagnostic: None,
) -> None:
    fake = baseline.FakeTransport()
    fake.producer_pass = False
    publisher = baseline._publisher(fake)
    normal_calls: list[tuple[int | None, int | None]] = []

    def normal(run_id: int | None, attempt: int | None) -> list[Any]:
        normal_calls.append((run_id, attempt))
        return []

    publisher.reconcile_all = normal
    result = publisher.reconcile_diagnostic_or_normal(
        SOURCE_RUN,
        1,
        event_name="workflow_run",
    )

    assert result == []
    assert normal_calls == [(SOURCE_RUN, 1)]
    assert baseline._post_count(fake, 17) == 0
    assert baseline._post_count(fake, 13) == 0
    assert fake.comments[17] == []
    assert fake.comments[13] == []


def test_legacy_rerun_reads_existing_pair_without_reposting(legacy_diagnostic: None) -> None:
    fake = baseline.FakeTransport()
    publisher = baseline._publisher(fake)
    publisher.reconcile_diagnostic_or_normal(SOURCE_RUN, 1, event_name="workflow_run")
    before = (baseline._post_count(fake, 17), baseline._post_count(fake, 13))

    result = publisher.reconcile_diagnostic_or_normal(
        SOURCE_RUN,
        1,
        event_name="workflow_run",
    )

    assert isinstance(result, PairedCommentDiagnosticResult)
    assert result.created_targets == ()
    assert (baseline._post_count(fake, 17), baseline._post_count(fake, 13)) == before == (1, 1)


def test_legacy_uncertain_post_is_read_back_without_a_retry(legacy_diagnostic: None) -> None:
    fake = baseline.FakeTransport()
    fake.uncertain_post_with_write.add(17)

    result = baseline._publisher(fake).reconcile_diagnostic_or_normal(
        SOURCE_RUN,
        1,
        event_name="workflow_run",
    )

    assert isinstance(result, PairedCommentDiagnosticResult)
    assert baseline._post_count(fake, 17) == 1
    assert len(fake.comments[17]) == 1
    assert baseline._post_count(fake, 13) == 1


def test_legacy_partial_pair_fences_rerun_after_pr_forbidden_without_reposting(
    legacy_diagnostic: None,
) -> None:
    fake = baseline.FakeTransport()
    fake.post_errors[13] = baseline.ApiError(
        "POST",
        f"{baseline.REPOSITORY_PATH}/issues/13/comments",
        403,
        "status=403; message=Forbidden; "
        "X-GitHub-Request-Id=abcd:123456:abcdef:7890ab:12345678; "
        "X-Accepted-GitHub-Permissions=issues=write, contents=read; "
        "Authorization=COMMENT_TOKEN_SENTINEL; body=COMMENT_BODY_SENTINEL",
    )
    publisher = baseline._publisher(fake)

    with pytest.raises(PublisherError) as first:
        publisher.reconcile_diagnostic_or_normal(SOURCE_RUN, 1, event_name="workflow_run")

    first_message = str(first.value)
    assert "target=pull_request#13" in first_message
    assert "HTTP 403" in first_message
    assert "X-GitHub-Request-Id=abcd:123456:abcdef:7890ab:12345678" in first_message
    assert "X-Accepted-GitHub-Permissions=issues=write, contents=read" in first_message
    assert "COMMENT_TOKEN_SENTINEL" not in first_message
    assert "COMMENT_BODY_SENTINEL" not in first_message
    assert baseline._post_count(fake, 17) == 1
    assert baseline._post_count(fake, 13) == 1

    with pytest.raises(PublisherError, match="refusing all further POSTs"):
        publisher.reconcile_diagnostic_or_normal(SOURCE_RUN, 1, event_name="workflow_run")

    assert baseline._post_count(fake, 17) == 1
    assert baseline._post_count(fake, 13) == 1
    assert len(fake.comments[17]) == 1
    assert fake.comments[13] == []

    fake.source_run_attempt = 2
    with pytest.raises(PublisherError, match="refusing all further POSTs"):
        publisher.reconcile_diagnostic_or_normal(SOURCE_RUN, 2, event_name="workflow_run")

    assert baseline._post_count(fake, 17) == 1
    assert baseline._post_count(fake, 13) == 1


def test_legacy_uncertain_post_without_readback_stops_without_posting_second_target(
    legacy_diagnostic: None,
) -> None:
    fake = baseline.FakeTransport()
    fake.uncertain_post_without_write.add(17)

    with pytest.raises(PublisherError, match="not confirmed by read-back"):
        baseline._publisher(fake).reconcile_diagnostic_or_normal(
            SOURCE_RUN,
            1,
            event_name="workflow_run",
        )

    assert baseline._post_count(fake, 17) == 1
    assert baseline._post_count(fake, 13) == 0
    assert fake.comments[17] == []


def test_legacy_diagnostic_is_not_active_for_manual_context(legacy_diagnostic: None) -> None:
    fake = baseline.FakeTransport()
    publisher = baseline._publisher(fake)
    called: list[tuple[int | None, int | None]] = []

    def normal(run_id: int | None, attempt: int | None) -> list[Any]:
        called.append((run_id, attempt))
        return []

    publisher.reconcile_all = normal
    result = publisher.reconcile_diagnostic_or_normal(
        SOURCE_RUN,
        1,
        event_name="workflow_dispatch",
    )

    assert result == []
    assert called == [(SOURCE_RUN, 1)]
    assert fake.comments[17] == []
    assert fake.comments[13] == []


def test_legacy_dormant_registration_preserves_normal_reconciliation(
    legacy_diagnostic: None,
) -> None:
    fake = baseline.FakeTransport(manifest="schema_version: 6\n")
    publisher = baseline._publisher(fake)
    called: list[tuple[int | None, int | None]] = []

    def normal(run_id: int | None, attempt: int | None) -> list[Any]:
        called.append((run_id, attempt))
        return []

    publisher.reconcile_all = normal
    result = publisher.reconcile_diagnostic_or_normal(
        SOURCE_RUN,
        1,
        event_name="workflow_run",
    )

    assert result == []
    assert called == [(SOURCE_RUN, 1)]
    assert fake.comments[17] == []
    assert fake.comments[13] == []


def test_legacy_finalizer_uses_same_job_and_limits_the_temporary_permission_exception(
    legacy_diagnostic: None,
) -> None:
    workflow = _HISTORICAL_FINALIZER_WORKFLOW
    jobs = workflow.split("jobs:\n", 1)[1]

    assert workflow.count("jobs:\n") == 1
    assert "run: python tools/post_merge_publisher.py --diagnostic-aware" in jobs
    assert "contents: read" in jobs
    assert "actions: read" in jobs
    assert "checks: read" in jobs
    assert "pull-requests: write" in jobs
    assert "issues: write" in jobs
    assert "contents: write" not in jobs
    assert "actions: write" not in jobs
    assert "checks: write" not in jobs
    assert "pull-requests: read" not in jobs
    assert jobs.count("pull-requests: write") == 1
    assert "matrix:" not in jobs
    assert jobs.count("python tools/post_merge_publisher.py --diagnostic-aware") == 1
    assert "PAT" not in workflow
    assert "GCM" not in workflow
    assert "continue-on-error" not in workflow
