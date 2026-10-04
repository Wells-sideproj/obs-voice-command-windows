from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import tools.w11_010_comment_probe as probe_module

from tools.w11_010_comment_probe import (
    CANONICAL_REPOSITORY,
    COMMENT_MARKER,
    ISSUE_MARKER,
    ApiError,
    GithubApi,
    ProbeConfig,
    ProbeError,
    ProbeFailure,
    config_from_environment,
    run_probe,
)


ROOT = Path(__file__).resolve().parents[3]
REPOSITORY_PATH = f"/repos/{CANONICAL_REPOSITORY}"
TOKEN = "COMMENT_PROBE_SECRET_SENTINEL"
SHA = "a" * 40
BASE_CONFIG = ProbeConfig(
    repository=CANONICAL_REPOSITORY,
    token=TOKEN,
    event_name="workflow_dispatch",
    ref="refs/heads/develop",
    sha=SHA,
    run_id=12345,
    run_attempt=1,
)
BOT = {"login": "github-actions[bot]", "type": "Bot"}


class FakeTransport:
    def __init__(self) -> None:
        self.issues: list[dict[str, Any]] = []
        self.comments_by_issue: dict[int, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.next_issue_number = 140
        self.next_comment_id = 900
        self.failures: dict[tuple[str, str], list[Exception | None]] = {}

    def fail_next(
        self,
        method: str,
        path_prefix: str,
        error: Exception,
        *,
        skip: int = 0,
    ) -> None:
        self.failures[(method, path_prefix)] = [None] * skip + [error]

    def _maybe_fail(self, method: str, path: str) -> None:
        base_path = path.split("?", 1)[0]
        for (failure_method, prefix), failures in list(self.failures.items()):
            if failure_method != method or not base_path.startswith(prefix):
                continue
            if not failures:
                continue
            failure = failures.pop(0)
            if not failures:
                del self.failures[(failure_method, prefix)]
            if failure is not None:
                raise failure
            return

    def _issue(self, number: int) -> dict[str, Any]:
        for issue in self.issues:
            if issue["number"] == number:
                return issue
        raise AssertionError(f"unknown issue #{number}")

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append((method, path, payload))
        self._maybe_fail(method, path)
        base_path = path.split("?", 1)[0]
        if method == "GET" and base_path == REPOSITORY_PATH:
            return {"full_name": CANONICAL_REPOSITORY, "private": False}
        if method == "GET" and base_path == f"{REPOSITORY_PATH}/issues":
            return list(self.issues) if "page=1" in path else []
        if method == "POST" and base_path == f"{REPOSITORY_PATH}/issues":
            assert payload is not None
            issue = {
                "number": self.next_issue_number,
                "state": "open",
                "body": payload["body"],
                "title": payload["title"],
                "html_url": f"https://github.com/{CANONICAL_REPOSITORY}/issues/{self.next_issue_number}",
                "user": BOT,
            }
            self.next_issue_number += 1
            self.issues.append(issue)
            self.comments_by_issue[issue["number"]] = []
            return dict(issue)
        match = re.fullmatch(rf"{re.escape(REPOSITORY_PATH)}/issues/(\d+)", base_path)
        if method == "GET" and match:
            return dict(self._issue(int(match.group(1))))
        if method == "PATCH" and match:
            assert payload == {"state": "closed"}
            issue = self._issue(int(match.group(1)))
            issue["state"] = "closed"
            return dict(issue)
        comments_match = re.fullmatch(
            rf"{re.escape(REPOSITORY_PATH)}/issues/(\d+)/comments",
            base_path,
        )
        if method == "GET" and comments_match:
            return list(self.comments_by_issue[int(comments_match.group(1))])
        if method == "POST" and comments_match:
            assert payload is not None
            number = int(comments_match.group(1))
            comment = {
                "id": self.next_comment_id,
                "body": payload["body"],
                "user": BOT,
            }
            self.next_comment_id += 1
            self.comments_by_issue[number].append(comment)
            return dict(comment)
        raise AssertionError(f"unexpected fake request: {method} {path}")


def _api(fake: FakeTransport) -> GithubApi:
    return GithubApi(fake, CANONICAL_REPOSITORY)


def _count_calls(fake: FakeTransport, method: str, suffix: str) -> int:
    return sum(
        call_method == method and call_path.split("?", 1)[0].endswith(suffix)
        for call_method, call_path, _ in fake.calls
    )


def test_first_run_creates_one_public_issue_one_comment_and_closes_it() -> None:
    fake = FakeTransport()

    result = run_probe(_api(fake), BASE_CONFIG)

    assert result.as_dict() == {
        "status": "success",
        "namespace": "w11-010-comment-probe",
        "issue_number": 140,
        "issue_url": f"https://github.com/{CANONICAL_REPOSITORY}/issues/140",
        "created_issue": True,
        "comment_posted": True,
        "comment_verified": True,
        "issue_closed": True,
        "persistent_closed_issue": True,
        "deleted": False,
    }
    assert fake.issues[0]["state"] == "closed"
    assert ISSUE_MARKER in fake.issues[0]["body"]
    assert fake.issues[0]["user"] == BOT
    assert len(fake.comments_by_issue[140]) == 1
    assert COMMENT_MARKER in fake.comments_by_issue[140][0]["body"]
    assert fake.comments_by_issue[140][0]["user"] == BOT
    assert _count_calls(fake, "POST", "/issues") == 1
    assert _count_calls(fake, "POST", "/comments") == 1
    assert _count_calls(fake, "GET", "/comments") >= 2
    assert any(
        method == "PATCH" and path.endswith("/issues/140")
        for method, path, _ in fake.calls
    )


def test_rerun_reuses_closed_issue_and_marker_without_duplicate_writes() -> None:
    fake = FakeTransport()
    first = run_probe(_api(fake), BASE_CONFIG)
    counts_after_first = {
        "issue": _count_calls(fake, "POST", "/issues"),
        "comment": _count_calls(fake, "POST", "/comments"),
        "close": _count_calls(fake, "PATCH", "/issues/140"),
    }

    second = run_probe(_api(fake), replace(BASE_CONFIG, run_attempt=2))

    assert first.issue_number == second.issue_number == 140
    assert second.created_issue is False
    assert second.created_comment is False
    assert second.comment_verified is True
    assert fake.issues[0]["state"] == "closed"
    assert len(fake.comments_by_issue[140]) == 1
    assert {
        "issue": _count_calls(fake, "POST", "/issues"),
        "comment": _count_calls(fake, "POST", "/comments"),
        "close": _count_calls(fake, "PATCH", "/issues/140"),
    } == counts_after_first


@pytest.mark.parametrize("failure_kind", ["create", "comment", "readback"])
def test_fake_transport_failures_are_sanitized_and_cleanup_is_attempted(
    failure_kind: str,
) -> None:
    fake = FakeTransport()
    if failure_kind == "create":
        fake.fail_next("POST", f"{REPOSITORY_PATH}/issues", ApiError(
            "transport", 403, "Forbidden"
        ))
    elif failure_kind == "comment":
        fake.fail_next("POST", f"{REPOSITORY_PATH}/issues/140/comments", ApiError(
            "transport", 403, "Forbidden"
        ))
    else:
        fake.fail_next("GET", f"{REPOSITORY_PATH}/issues/140/comments", ApiError(
            "transport", 403, "Forbidden"
        ), skip=1)

    with pytest.raises(ProbeFailure) as raised:
        run_probe(_api(fake), BASE_CONFIG)

    evidence = raised.value.as_dict()
    assert evidence["status"] == "failure"
    assert evidence["namespace"] == "w11-010-comment-probe"
    assert "HTTP 403" in evidence["error"]
    assert evidence["deleted"] is False
    if failure_kind == "create":
        assert "issue_number" not in evidence
        assert evidence["cleanup"].startswith("not attempted")
    else:
        assert evidence["issue_number"] == 140
        assert evidence["cleanup"] == "closed issue #140"
        assert fake.issues[0]["state"] == "closed"


def test_http_operation_labels_distinguish_issue_and_comment_without_secrets() -> None:
    hostile_secret = "OPERATION_HOSTILE_SECRET_SENTINEL"
    issue_fake = FakeTransport()
    issue_fake.fail_next(
        "POST",
        f"{REPOSITORY_PATH}/issues",
        ApiError("transport", 403, hostile_secret),
    )

    with pytest.raises(ProbeFailure) as issue_raised:
        run_probe(_api(issue_fake), BASE_CONFIG)
    issue_evidence = json.dumps(issue_raised.value.as_dict(), sort_keys=True)

    comment_fake = FakeTransport()
    comment_fake.fail_next(
        "POST",
        f"{REPOSITORY_PATH}/issues/140/comments",
        ApiError("transport", 403, hostile_secret),
    )

    with pytest.raises(ProbeFailure) as comment_raised:
        run_probe(_api(comment_fake), BASE_CONFIG)
    comment_evidence = json.dumps(comment_raised.value.as_dict(), sort_keys=True)

    assert "operation=create_issue" in issue_evidence
    assert "operation=create_comment" in comment_evidence
    assert "HTTP 403" in issue_evidence
    assert "HTTP 403" in comment_evidence
    assert hostile_secret not in issue_evidence
    assert hostile_secret not in comment_evidence


def test_unrelated_bot_marker_owner_is_rejected_without_new_writes() -> None:
    fake = FakeTransport()
    run_probe(_api(fake), BASE_CONFIG)
    write_counts = {
        "issue": _count_calls(fake, "POST", "/issues"),
        "comment": _count_calls(fake, "POST", "/comments"),
        "close": _count_calls(fake, "PATCH", "/issues/140"),
    }
    fake.comments_by_issue[140][0]["user"] = {
        "login": "unrelated[bot]",
        "type": "Bot",
    }

    with pytest.raises(ProbeFailure) as raised:
        run_probe(_api(fake), replace(BASE_CONFIG, run_attempt=2))

    evidence = raised.value.as_dict()
    assert evidence["error"] == "probe_error"
    assert evidence["cleanup"] == "already closed issue #140"
    assert {
        "issue": _count_calls(fake, "POST", "/issues"),
        "comment": _count_calls(fake, "POST", "/comments"),
        "close": _count_calls(fake, "PATCH", "/issues/140"),
    } == write_counts


def test_cleanup_failure_and_hostile_exception_text_are_not_disclosed() -> None:
    hostile_secret = "UNRELATED_HOSTILE_SECRET_SENTINEL"
    fake = FakeTransport()
    fake.fail_next(
        "POST",
        f"{REPOSITORY_PATH}/issues/140/comments",
        ProbeError(hostile_secret),
    )
    fake.fail_next(
        "PATCH",
        f"{REPOSITORY_PATH}/issues/140",
        ProbeError(hostile_secret),
    )

    with pytest.raises(ProbeFailure) as raised:
        run_probe(_api(fake), BASE_CONFIG)

    evidence = json.dumps(raised.value.as_dict(), sort_keys=True)
    assert hostile_secret not in evidence
    assert "probe_error" in evidence
    assert (
        "close failed: github_api_operation_error operation=close_issue "
        "category=probe_error"
    ) in evidence
    assert fake.issues[0]["state"] == "open"


def test_cli_generic_fallback_does_not_print_hostile_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hostile_secret = "CLI_HOSTILE_SECRET_SENTINEL"
    monkeypatch.setattr(probe_module, "config_from_environment", lambda: BASE_CONFIG)

    def fail_probe(*_: Any, **__: Any) -> Any:
        raise ProbeError(hostile_secret)

    monkeypatch.setattr(probe_module, "run_probe", fail_probe)

    assert probe_module.main([]) == 1
    captured = capsys.readouterr()
    assert hostile_secret not in captured.out
    assert hostile_secret not in captured.err
    assert "probe_error" in captured.err


@pytest.mark.parametrize("key,value", [
    ("GITHUB_EVENT_NAME", "pull_request"),
    ("GITHUB_REF", "refs/heads/feature"),
])
def test_context_guard_rejects_non_dispatch_or_non_develop(
    key: str,
    value: str,
) -> None:
    environment = {
        "GITHUB_REPOSITORY": CANONICAL_REPOSITORY,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/develop",
        "GITHUB_TOKEN": TOKEN,
        "GITHUB_SHA": SHA,
        "GITHUB_RUN_ID": "12345",
        "GITHUB_RUN_ATTEMPT": "1",
    }
    environment[key] = value

    with pytest.raises(ProbeError):
        config_from_environment(environment)


def test_workflow_is_dispatch_only_job_scoped_and_sha_pinned() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "w11-010-comment-probe.yml"
    ).read_text(encoding="utf-8")
    header, jobs = workflow.split("jobs:\n", 1)

    assert "workflow_dispatch:" in header
    assert "permissions: {}" in header
    assert "pull_request" not in workflow
    assert "merge_group" not in workflow
    assert "push:" not in workflow
    assert "workflow_run" not in workflow
    assert "workflow_call" not in workflow
    assert "permissions:\n      contents: read\n      issues: write" in jobs
    assert "contents: write" not in jobs
    assert "actions: write" not in jobs
    assert "pull-requests: write" not in jobs
    assert "GITHUB_TOKEN: ${{ github.token }}" in jobs
    assert "github.ref == 'refs/heads/develop'" in jobs
    assert "ref: ${{ github.sha }}" in jobs
    assert "ref: develop" not in jobs
    assert "git rev-parse HEAD" in jobs
    assert '[[ "$actual_sha" != "$GITHUB_SHA" ]]' in jobs
    assert "GITHUB_SHA must be a full commit SHA" in jobs
    assert "w11_010_comment_probe.py" in jobs
    assert "post_merge_publisher.py" not in jobs
    assert "PAT" not in workflow
    assert jobs.index("Verify checked out trusted SHA") < jobs.index(
        "Run bounded issue-comment probe"
    )
