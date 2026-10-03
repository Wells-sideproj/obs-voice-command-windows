from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

from tools.post_merge_publisher import (
    DEFAULT_REQUIRED_JOBS,
    PASS_MARKER,
    REPAIR_MARKER,
    GithubApi,
    HttpResponse,
    PostMergePublisher,
    PublisherConfig,
    PublisherError,
    SIMULATION_MARKER,
    SIMULATION_NAMESPACE,
    SimulationPublisher,
    lineage_key,
)


REPOSITORY = "Wells-sideproj/obs-voice-command-windows"
WORKFLOW_ID = 9010
SHA_ONE = "1" * 40
SHA_TWO = "2" * 40
SHA_THREE = "3" * 40
SHA_FOUR = "4" * 40
ROOT_PR = 101
REPAIR_PR = 102
THIRD_PR = 103
FOURTH_PR = 104
CODE_STEP_BY_JOB = {
    "layer-a / windows-unit": "Run Windows hardware-free tests",
    "layer-a / macos-regression": "Run macOS regression tests",
    "layer-a / package": "Build wheel and source distribution",
    "required / gate": "Require full stage and every Layer A job",
}


def _manifest(*, attempts: int = 1) -> str:
    lines = [
        "tickets:",
        "  - id: W11-010",
        "    post_merge_registration:",
        "      version: 1",
        "      attempts:",
        "        - pr_number: 101",
        "          predecessor_pr_number: null",
        "          repair_issue_number: null",
    ]
    for pr_number in range(REPAIR_PR, ROOT_PR + attempts):
        lines.extend(
            [
                f"        - pr_number: {pr_number}",
                f"          predecessor_pr_number: {pr_number - 1}",
                f"          repair_issue_number: {899 + pr_number - ROOT_PR}",
            ]
        )
    lines.extend(["  - id: W11-011", "    status: Blocked", ""])
    return "\n".join(lines)


def _manifest_with_layer_b_ticket() -> str:
    return _manifest().replace(
        "  - id: W11-011\n    status: Blocked",
        "\n".join(
            [
                "  - id: W11-011",
                "    completion_profile: layer_a_plus_layer_b_exact_sha",
                "    post_merge_registration:",
                "      version: 1",
                "      attempts:",
                "        - pr_number: 201",
                "          predecessor_pr_number: null",
                "          repair_issue_number: null",
            ]
        ),
    )


def _run(
    run_id: int,
    sha: str,
    *,
    conclusion: str = "success",
    attempt: int = 1,
    failed_job: str | None = None,
    failed_step: str | None = None,
    runner_failure: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for name in DEFAULT_REQUIRED_JOBS:
        failed = name == failed_job
        jobs.append(
            {
                "id": run_id * 10 + len(jobs),
                "name": name,
                "conclusion": "failure" if failed else "success",
                "steps": (
                    []
                    if failed and runner_failure
                    else [
                        {
                            "name": failed_step or CODE_STEP_BY_JOB[name],
                            "conclusion": "failure",
                        }
                    ]
                    if failed
                    else []
                ),
            }
        )
    run = {
        "id": run_id,
        "run_attempt": attempt,
        "repository": {"full_name": REPOSITORY},
        "workflow_id": WORKFLOW_ID,
        "event": "push",
        "head_branch": "develop",
        "status": "completed",
        "conclusion": conclusion,
        "head_sha": sha,
        "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
    }
    suite_id = run_id * 1000 + attempt
    run["check_suite_id"] = suite_id
    suite = {
        "id": suite_id,
        "head_sha": sha,
        "conclusion": conclusion,
        "app": {"slug": "github-actions", "name": "GitHub Actions"},
    }
    return run, jobs, suite


class FakeTransport:
    """HTTP-shaped fake that exercises GithubApi pagination and mutations."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.requests: list[tuple[str, str, Mapping[str, Any] | None, Mapping[str, Any] | None]] = []
        self.runs: dict[int, dict[str, Any]] = {}
        self.jobs: dict[int, list[dict[str, Any]]] = {}
        self.runs_by_attempt: dict[tuple[int, int], dict[str, Any]] = {}
        self.jobs_by_attempt: dict[tuple[int, int], list[dict[str, Any]]] = {}
        self.suites: dict[str, list[dict[str, Any]]] = {}
        self.pulls: dict[int, dict[str, Any]] = {}
        self.pulls_by_sha: dict[str, list[dict[str, Any]]] = {}
        self.manifests: dict[str, str] = {}
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.issues_data: list[dict[str, Any]] = []
        self.branch_tip_sha = SHA_ONE
        self.repository_full_name = REPOSITORY
        self.next_comment = 5000
        self.next_issue = 900
        self.failures: dict[tuple[str, str], tuple[int, Any]] = {}
        self.on_request: Callable[[str, str], None] | None = None

    def add_pull(self, number: int, sha: str) -> None:
        value = {
            "number": number,
            "merged_at": "2026-10-01T00:00:00Z",
            "merge_commit_sha": sha,
            "base": {
                "ref": "develop",
                "repo": {"full_name": REPOSITORY},
            },
            "html_url": f"https://github.com/{REPOSITORY}/pull/{number}",
        }
        self.pulls[number] = value
        self.pulls_by_sha[sha] = [{"number": number}]
        self.comments.setdefault(number, [])

    def add_run(
        self,
        run_id: int,
        sha: str,
        *,
        conclusion: str = "success",
        attempt: int = 1,
        failed_job: str | None = None,
        failed_step: str | None = None,
        runner_failure: bool = False,
    ) -> None:
        run, jobs, suite = _run(
            run_id,
            sha,
            conclusion=conclusion,
            attempt=attempt,
            failed_job=failed_job,
            failed_step=failed_step,
            runner_failure=runner_failure,
        )
        self.runs[run_id] = run
        self.jobs[run_id] = jobs
        self.runs_by_attempt[(run_id, attempt)] = dict(run)
        self.jobs_by_attempt[(run_id, attempt)] = [dict(job) for job in jobs]
        self.suites.setdefault(sha, []).append(suite)

    def _response(self, body: Any, status: int = 200) -> HttpResponse:
        return HttpResponse(status=status, body=body, headers={})

    @staticmethod
    def _page(values: list[Any], params: Mapping[str, Any] | None) -> list[Any]:
        page = int((params or {}).get("page", 1))
        start = (page - 1) * 100
        return values[start : start + 100]

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json_body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        del headers
        with self.lock:
            self.requests.append((method, path, params, json_body))
            if self.on_request is not None:
                self.on_request(method, path)
            prefix = f"/repos/{REPOSITORY}"
            if method == "GET" and path == prefix:
                return self._response({"full_name": self.repository_full_name})
            failure = self.failures.get((method, path))
            if failure is not None:
                status, body = failure
                return self._response(body, status=status)
            if method == "GET" and path == f"{prefix}/actions/workflows/post-merge.yml":
                return self._response(
                    {
                        "id": WORKFLOW_ID,
                        "path": ".github/workflows/post-merge.yml",
                    }
                )
            if method == "GET" and path == f"{prefix}/git/ref/heads/develop":
                return self._response({"object": {"sha": self.branch_tip_sha}})
            match = re.fullmatch(rf"{re.escape(prefix)}/actions/runs/(\d+)/attempts/(\d+)", path)
            if method == "GET" and match:
                run_id = int(match.group(1))
                attempt = int(match.group(2))
                if attempt == self.runs[run_id]["run_attempt"]:
                    return self._response(self.runs[run_id])
                value = self.runs_by_attempt.get((run_id, attempt))
                if value is None:
                    return self._response({"message": "attempt not found"}, status=404)
                return self._response(value)
            match = re.fullmatch(rf"{re.escape(prefix)}/actions/runs/(\d+)/attempts/(\d+)/jobs", path)
            if method == "GET" and match:
                run_id = int(match.group(1))
                attempt = int(match.group(2))
                if attempt == self.runs[run_id]["run_attempt"]:
                    values = self.jobs[run_id]
                else:
                    values = self.jobs_by_attempt.get((run_id, attempt))
                if values is None:
                    return self._response({"message": "attempt not found"}, status=404)
                return self._response({"jobs": self._page(values, params)})
            match = re.fullmatch(rf"{re.escape(prefix)}/actions/runs/(\d+)", path)
            if method == "GET" and match:
                return self._response(self.runs[int(match.group(1))])
            match = re.fullmatch(rf"{re.escape(prefix)}/actions/runs/(\d+)/jobs", path)
            if method == "GET" and match:
                return self._response({"jobs": self._page(self.jobs[int(match.group(1))], params)})
            match = re.fullmatch(rf"{re.escape(prefix)}/actions/workflows/{WORKFLOW_ID}/runs", path)
            if method == "GET" and match:
                return self._response({"workflow_runs": self._page(list(self.runs.values()), params)})
            match = re.fullmatch(rf"{re.escape(prefix)}/commits/([0-9a-f]+)/check-suites", path)
            if method == "GET" and match:
                return self._response({"check_suites": self._page(self.suites.get(match.group(1), []), params)})
            match = re.fullmatch(rf"{re.escape(prefix)}/commits/([0-9a-f]+)/pulls", path)
            if method == "GET" and match:
                return self._response(self.pulls_by_sha.get(match.group(1), []))
            match = re.fullmatch(rf"{re.escape(prefix)}/pulls/(\d+)", path)
            if method == "GET" and match:
                return self._response(self.pulls[int(match.group(1))])
            match = re.fullmatch(rf"{re.escape(prefix)}/compare/([0-9a-f]+)\.\.\.([0-9a-f]+)", path)
            if method == "GET" and match:
                return self._response(
                    {"status": "identical" if match.group(1) == match.group(2) else "ahead"}
                )
            match = re.fullmatch(rf"{re.escape(prefix)}/contents/(.+)", path)
            if method == "GET" and match:
                ref = str((params or {}).get("ref", ""))
                manifest = self.manifests[ref]
                encoded = base64.b64encode(manifest.encode("utf-8")).decode("ascii")
                encoded = "\n".join(encoded[index : index + 76] for index in range(0, len(encoded), 76))
                return self._response({"type": "file", "encoding": "base64", "content": encoded})
            if method == "GET" and path == f"{prefix}/issues":
                return self._response(self._page(self.issues_data, params))
            if method == "POST" and path == f"{prefix}/issues":
                assert json_body is not None
                number = self.next_issue
                self.next_issue += 1
                issue = {
                    "number": number,
                    "body": json_body["body"],
                    "title": json_body["title"],
                    "labels": json_body.get("labels", []),
                    "state": "open",
                    "user": {"type": "Bot", "login": "github-actions[bot]"},
                    "html_url": f"https://github.com/{REPOSITORY}/issues/{number}",
                }
                self.issues_data.append(issue)
                return self._response(issue, status=201)
            match = re.fullmatch(rf"{re.escape(prefix)}/issues/(\d+)/comments", path)
            if method == "GET" and match:
                return self._response(self._page(self.comments[int(match.group(1))], params))
            if method == "POST" and match:
                assert json_body is not None
                comment = {
                    "id": self.next_comment,
                    "body": json_body["body"],
                    "user": {"type": "Bot", "login": "github-actions[bot]"},
                }
                self.next_comment += 1
                self.comments[int(match.group(1))].append(comment)
                return self._response(comment, status=201)
            match = re.fullmatch(rf"{re.escape(prefix)}/issues/(\d+)", path)
            if method == "PATCH" and match:
                assert json_body is not None
                issue = next(issue for issue in self.issues_data if issue["number"] == int(match.group(1)))
                issue.update(json_body)
                return self._response(issue)
            match = re.fullmatch(rf"{re.escape(prefix)}/issues/(\d+)/labels", path)
            if method == "PUT" and match:
                assert json_body is not None
                issue = next(issue for issue in self.issues_data if issue["number"] == int(match.group(1)))
                issue["labels"] = json_body["labels"]
                return self._response([{"name": label} for label in issue["labels"]])
            match = re.fullmatch(rf"{re.escape(prefix)}/issues/comments/(\d+)", path)
            if method == "PATCH" and match:
                assert json_body is not None
                for comments in self.comments.values():
                    for comment in comments:
                        if comment["id"] == int(match.group(1)):
                            comment.update(json_body)
                            return self._response(comment)
            raise AssertionError(f"unhandled fake request: {method} {path} {params} {json_body}")


def _scenario(
    *,
    conclusion: str = "success",
    failed_job: str | None = None,
    failed_step: str | None = None,
    runner_failure: bool = False,
) -> FakeTransport:
    transport = FakeTransport()
    transport.add_pull(ROOT_PR, SHA_ONE)
    transport.manifests[SHA_ONE] = _manifest()
    transport.manifests[SHA_ONE] = _manifest()
    transport.add_run(
        5001,
        SHA_ONE,
        conclusion=conclusion,
        failed_job=failed_job,
        failed_step=failed_step,
        runner_failure=runner_failure,
    )
    return transport


def _publisher(transport: FakeTransport) -> PostMergePublisher:
    return PostMergePublisher(
        GithubApi(transport, REPOSITORY),
        PublisherConfig(repository=REPOSITORY),
    )


def _simulation_publisher(transport: FakeTransport) -> SimulationPublisher:
    return SimulationPublisher(
        GithubApi(transport, REPOSITORY),
        PublisherConfig(repository=REPOSITORY),
    )


def _repair_issue_body(lineage: str) -> str:
    payload = {
        "kind": "w11-010-repair",
        "version": 1,
        "repository": REPOSITORY,
        "ticket_id": "W11-010",
        "lineage_id": lineage,
        "source_pr_number": REPAIR_PR,
        "source_sha": SHA_TWO,
        "source_run_id": 5002,
        "source_run_attempt": 1,
        "failure_signature": "a" * 64,
        "failing_jobs": {"layer-a / windows-unit": "failure"},
        "failed_steps": ["layer-a / windows-unit: Run Windows hardware-free tests"],
        "latest_merged_pr": REPAIR_PR,
        "rollback_hint": "Revert merged PR #102 through protected develop; never direct-push.",
        "consecutive_code_failures": 1,
    }
    return (
        f"{REPAIR_MARKER}\n```json\n"
        f"{json.dumps(payload, sort_keys=True, separators=(',', ':'))}\n```\n"
    )


def test_initial_pass_uses_registered_pr_without_precomputed_hashes() -> None:
    transport = _scenario()

    result = _publisher(transport).reconcile(5001, 1)

    assert result.status == "pass"
    assert result.action == "done_candidate"
    assert result.source_sha == SHA_ONE
    assert len(transport.comments[ROOT_PR]) == 1
    assert transport.issues_data == []
    assert any(method == "POST" and path.endswith("/issues/101/comments") for method, path, _, _ in transport.requests)


def test_realistic_check_suite_fixture_and_filename_selector_reach_publisher_entrypoint() -> None:
    transport = _scenario()

    result = _publisher(transport).reconcile(5001, 1)

    assert result.status == "pass"
    assert transport.suites[SHA_ONE][0]["id"] == transport.runs[5001]["check_suite_id"]
    assert "workflow_run" not in transport.suites[SHA_ONE][0]
    assert any(
        method == "GET"
        and path == f"/repos/{REPOSITORY}/actions/workflows/post-merge.yml"
        for method, path, _, _ in transport.requests
    )
    assert not any(
        "/actions/workflows/.github/workflows/post-merge.yml" in path
        for _, path, _, _ in transport.requests
    )


def test_finalizer_reconciles_all_registered_runs_from_protected_tip() -> None:
    transport = _scenario()

    results = _publisher(transport).reconcile_all()

    assert [result.status for result in results] == ["pass"]
    assert results[0].source_sha == SHA_ONE
    assert len(transport.comments[ROOT_PR]) == 1


def test_isolated_simulation_fail_then_rerun_uses_one_non_production_issue() -> None:
    transport = FakeTransport()
    publisher = _simulation_publisher(transport)

    failed = publisher.reconcile("fail")
    rerun = publisher.reconcile("rerun")

    assert failed.created_issue is True
    assert rerun.created_issue is False
    assert failed.issue_number == rerun.issue_number
    assert rerun.consecutive_code_failures == 1
    assert rerun.observations == ("fail", "rerun")
    assert len(transport.issues_data) == 1
    assert transport.issues_data[0]["labels"] == []
    assert "type:repair" not in transport.issues_data[0]["labels"]
    assert not transport.comments
    assert SIMULATION_MARKER in transport.issues_data[0]["body"]
    assert SIMULATION_NAMESPACE in transport.issues_data[0]["body"]
    assert not any(
        "/contents/" in path or "/pulls/" in path or "/comments" in path
        for _, path, _, _ in transport.requests
    )


def test_isolated_simulation_pass_closes_only_simulation_issue() -> None:
    transport = FakeTransport()

    result = _simulation_publisher(transport).reconcile("pass")

    assert result.created_issue is True
    assert transport.issues_data[0]["state"] == "closed"
    assert transport.issues_data[0]["labels"] == []
    assert not transport.comments


def test_isolated_simulation_rerun_without_failure_fails_closed() -> None:
    with pytest.raises(PublisherError, match="existing simulated failure"):
        _simulation_publisher(FakeTransport()).reconcile("rerun")


def test_cli_none_simulation_env_uses_ordinary_run_id_path() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_TOKEN": "unused-test-token",
            "POST_MERGE_SIMULATION_CASE": "none",
            "POST_MERGE_RUN_ID": "not-a-run-id",
        }
    )
    result = subprocess.run(
        [sys.executable, "tools/post_merge_publisher.py"],
        cwd=Path(__file__).resolve().parents[3],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 1
    assert "POST_MERGE_RUN_ID must be a positive integer" in result.stderr
    assert "simulation projection" not in result.stderr


def test_cli_simulation_is_blocked_outside_develop_manual_dispatch() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_TOKEN": "unused-test-token",
            "GITHUB_EVENT_NAME": "workflow_run",
            "GITHUB_REF": "refs/heads/develop",
            "POST_MERGE_SIMULATION_CASE": "pass",
        }
    )
    result = subprocess.run(
        [sys.executable, "tools/post_merge_publisher.py"],
        cwd=Path(__file__).resolve().parents[3],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 1
    assert "simulation requires a workflow_dispatch event" in result.stderr


def test_first_failure_creates_one_repair_and_rerun_reuses_it() -> None:
    transport = _scenario(
        conclusion="failure",
        failed_job="layer-a / windows-unit",
        failed_step="Run Windows hardware-free tests",
    )
    publisher = _publisher(transport)

    first = publisher.reconcile(5001, 1)
    second = publisher.reconcile(5001, 1)

    assert first.status == "failure"
    assert first.created_repair_issue is True
    assert second.created_repair_issue is False
    assert first.repair_issue_number == second.repair_issue_number == 900
    assert first.consecutive_code_failures == second.consecutive_code_failures == 1
    assert len(transport.issues_data) == 1
    assert first.evidence["failed_steps"] == ["layer-a / windows-unit: Run Windows hardware-free tests"]
    assert sum(method == "POST" and path.endswith("/issues") for method, path, _, _ in transport.requests) == 1


def test_failed_setup_is_infrastructure_blocked_without_repair_or_retry() -> None:
    transport = _scenario(
        conclusion="failure",
        failed_job="layer-a / windows-unit",
        failed_step="Install locked project",
    )

    result = _publisher(transport).reconcile(5001, 1)

    assert result.status == "blocked"
    assert result.action == "infrastructure_blocked"
    assert result.consecutive_code_failures == 0
    assert result.infrastructure_failures == 1
    assert result.repair_issue_number is None
    assert result.created_repair_issue is False
    assert transport.issues_data == []
    assert result.evidence["failed_steps"] == ["layer-a / windows-unit: Install locked project"]


def test_package_failure_is_infrastructure_blocked_without_repair_or_retry() -> None:
    transport = _scenario(
        conclusion="failure",
        failed_job="layer-a / package",
        failed_step="Build wheel and source distribution",
    )

    result = _publisher(transport).reconcile(5001, 1)

    assert result.status == "blocked"
    assert result.action == "infrastructure_blocked"
    assert result.consecutive_code_failures == 0
    assert result.infrastructure_failures == 1
    assert result.repair_issue_number is None
    assert result.created_repair_issue is False
    assert transport.issues_data == []
    assert result.evidence["failed_steps"] == ["layer-a / package: Build wheel and source distribution"]


def test_runner_failure_without_failed_steps_is_infrastructure_blocked() -> None:
    transport = _scenario(
        conclusion="failure",
        failed_job="layer-a / windows-unit",
        runner_failure=True,
    )

    result = _publisher(transport).reconcile(5001, 1)

    assert result.status == "blocked"
    assert result.action == "infrastructure_blocked"
    assert result.consecutive_code_failures == 0
    assert result.infrastructure_failures == 1
    assert result.repair_issue_number is None
    assert result.created_repair_issue is False
    assert transport.issues_data == []


def test_gate_failure_without_direct_code_failure_is_infrastructure_blocked() -> None:
    transport = _scenario(conclusion="failure", failed_job="required / gate")

    result = _publisher(transport).reconcile(5001, 1)

    assert result.status == "blocked"
    assert result.action == "infrastructure_blocked"
    assert result.consecutive_code_failures == 0
    assert result.repair_issue_number is None
    assert transport.issues_data == []


def test_retry_state_skips_infrastructure_attempt_between_code_failures() -> None:
    transport = FakeTransport()
    transport.add_pull(ROOT_PR, SHA_ONE)
    transport.add_pull(REPAIR_PR, SHA_TWO)
    transport.add_pull(THIRD_PR, SHA_THREE)
    transport.branch_tip_sha = SHA_THREE
    transport.manifests[SHA_ONE] = _manifest(attempts=1)
    transport.manifests[SHA_THREE] = _manifest(attempts=3)
    transport.add_run(
        5001,
        SHA_ONE,
        conclusion="failure",
        failed_job="layer-a / windows-unit",
        failed_step="Run Windows hardware-free tests",
    )
    transport.add_run(
        5002,
        SHA_TWO,
        conclusion="failure",
        failed_job="layer-a / windows-unit",
        failed_step="Install locked project",
    )
    transport.add_run(
        5003,
        SHA_THREE,
        conclusion="failure",
        failed_job="layer-a / windows-unit",
        failed_step="Run Windows hardware-free tests",
    )

    result = _publisher(transport).reconcile(5003, 1)

    assert result.status == "failure"
    assert result.consecutive_code_failures == 2
    assert result.infrastructure_failures == 1
    assert result.created_repair_issue is True
    assert len(transport.issues_data) == 1


def test_same_sha_infrastructure_rerun_does_not_mask_code_failure() -> None:
    transport = _scenario(
        conclusion="failure",
        failed_job="layer-a / windows-unit",
        failed_step="Run Windows hardware-free tests",
    )
    transport.add_run(
        5002,
        SHA_ONE,
        conclusion="failure",
        failed_job="layer-a / windows-unit",
        runner_failure=True,
    )
    transport.add_run(
        5003,
        SHA_ONE,
        conclusion="failure",
        failed_job="layer-a / windows-unit",
        failed_step="Run Windows hardware-free tests",
    )

    result = _publisher(transport).reconcile(5003, 1)

    assert result.status == "failure"
    assert result.consecutive_code_failures == 1
    assert result.infrastructure_failures == 1
    assert result.created_repair_issue is True
    assert len(transport.issues_data) == 1


def test_same_run_id_attempt_history_handles_code_infra_pass_and_delayed_event() -> None:
    transport = _scenario(
        conclusion="failure",
        failed_job="layer-a / windows-unit",
        failed_step="Run Windows hardware-free tests",
    )
    transport.add_run(
        5001,
        SHA_ONE,
        conclusion="failure",
        attempt=2,
        failed_job="layer-a / windows-unit",
        runner_failure=True,
    )
    transport.add_run(5001, SHA_ONE, conclusion="success", attempt=3)

    latest = _publisher(transport).reconcile(5001, 3)
    delayed = _publisher(transport).reconcile(5001, 1)

    assert latest.status == "pass"
    assert latest.consecutive_code_failures == 0
    assert delayed.status == "historical_failure"
    assert delayed.action == "no_mutation_stale_result"
    assert delayed.consecutive_code_failures == 0
    assert transport.issues_data == []
    assert len(transport.comments[ROOT_PR]) == 1
    assert any(
        method == "GET"
        and path.endswith("/actions/runs/5001/attempts/2/jobs")
        for method, path, _, _ in transport.requests
    )


def test_same_run_id_delayed_pass_cannot_close_newer_failure() -> None:
    transport = _scenario()
    transport.add_run(
        5001,
        SHA_ONE,
        conclusion="failure",
        attempt=2,
        failed_job="layer-a / windows-unit",
        failed_step="Run Windows hardware-free tests",
    )

    newer_failure = _publisher(transport).reconcile(5001, 2)
    delayed_pass = _publisher(transport).reconcile(5001, 1)

    assert newer_failure.status == "failure"
    assert newer_failure.created_repair_issue is True
    assert delayed_pass.status == "historical_pass"
    assert delayed_pass.action == "no_mutation_stale_result"
    assert transport.issues_data[0]["state"] == "open"
    assert transport.comments[ROOT_PR] == []


def test_missing_same_run_attempt_evidence_blocks_retry_accounting() -> None:
    transport = _scenario(
        conclusion="failure",
        failed_job="layer-a / windows-unit",
        failed_step="Run Windows hardware-free tests",
    )
    transport.add_run(
        5001,
        SHA_ONE,
        conclusion="failure",
        attempt=3,
        failed_job="layer-a / windows-unit",
        failed_step="Run Windows hardware-free tests",
    )

    with pytest.raises(PublisherError, match="retry accounting is blocked"):
        _publisher(transport).reconcile(5001, 3)
    assert transport.issues_data == []


def test_same_sha_delayed_pass_cannot_close_newer_failure() -> None:
    transport = _scenario()
    transport.add_run(
        5002,
        SHA_ONE,
        conclusion="failure",
        failed_job="layer-a / windows-unit",
        failed_step="Run Windows hardware-free tests",
    )

    newer_failure = _publisher(transport).reconcile(5002, 1)
    delayed_pass = _publisher(transport).reconcile(5001, 1)

    assert newer_failure.status == "failure"
    assert newer_failure.created_repair_issue is True
    assert delayed_pass.status == "historical_pass"
    assert delayed_pass.action == "no_mutation_stale_result"
    assert delayed_pass.consecutive_code_failures == 1
    assert transport.issues_data[0]["state"] == "open"
    assert transport.comments[ROOT_PR] == []
    assert sum(
        method == "POST" and path.endswith("/issues")
        for method, path, _, _ in transport.requests
    ) == 1


def test_same_sha_delayed_failure_cannot_reopen_after_newer_pass() -> None:
    transport = _scenario(
        conclusion="failure",
        failed_job="layer-a / windows-unit",
        failed_step="Run Windows hardware-free tests",
    )
    transport.add_run(5002, SHA_ONE, conclusion="success")

    newer_pass = _publisher(transport).reconcile(5002, 1)
    delayed_failure = _publisher(transport).reconcile(5001, 1)

    assert newer_pass.status == "pass"
    assert newer_pass.action == "done_candidate"
    assert delayed_failure.status == "historical_failure"
    assert delayed_failure.action == "no_mutation_stale_result"
    assert delayed_failure.consecutive_code_failures == 0
    assert transport.issues_data == []
    assert len(transport.comments[ROOT_PR]) == 1
    assert sum(
        method == "POST" and path.endswith("/issues")
        for method, path, _, _ in transport.requests
    ) == 0


def test_infrastructure_projection_preserves_prior_code_failures() -> None:
    transport = FakeTransport()
    transport.add_pull(ROOT_PR, SHA_ONE)
    transport.add_pull(REPAIR_PR, SHA_TWO)
    transport.branch_tip_sha = SHA_TWO
    transport.manifests[SHA_ONE] = _manifest(attempts=1)
    transport.manifests[SHA_TWO] = _manifest(attempts=2)
    transport.add_run(
        5001,
        SHA_ONE,
        conclusion="failure",
        failed_job="layer-a / windows-unit",
        failed_step="Run Windows hardware-free tests",
    )
    transport.add_run(
        5002,
        SHA_TWO,
        conclusion="failure",
        failed_job="layer-a / windows-unit",
        failed_step="Install locked project",
    )

    result = _publisher(transport).reconcile(5002, 1)

    assert result.status == "blocked"
    assert result.action == "infrastructure_blocked"
    assert result.consecutive_code_failures == 1
    assert result.infrastructure_failures == 1
    assert result.repair_issue_number is None
    assert result.created_repair_issue is False
    assert result.evidence["verified_lineage_code_failures"] == 1
    assert transport.issues_data == []


def test_later_genuine_pass_resets_code_failures_after_infrastructure_gap() -> None:
    transport = FakeTransport()
    transport.add_pull(ROOT_PR, SHA_ONE)
    transport.add_pull(REPAIR_PR, SHA_TWO)
    transport.add_pull(THIRD_PR, SHA_THREE)
    transport.add_pull(FOURTH_PR, SHA_FOUR)
    transport.branch_tip_sha = SHA_THREE
    transport.manifests[SHA_ONE] = _manifest(attempts=1)
    transport.manifests[SHA_THREE] = _manifest(attempts=3)
    transport.add_run(
        5001,
        SHA_ONE,
        conclusion="failure",
        failed_job="layer-a / windows-unit",
        failed_step="Run Windows hardware-free tests",
    )
    transport.add_run(
        5002,
        SHA_TWO,
        conclusion="failure",
        failed_job="layer-a / windows-unit",
        failed_step="Install locked project",
    )
    transport.add_run(
        5003,
        SHA_THREE,
        conclusion="failure",
        failed_job="layer-a / windows-unit",
        failed_step="Run Windows hardware-free tests",
    )
    failed = _publisher(transport).reconcile(5003, 1)

    transport.branch_tip_sha = SHA_FOUR
    transport.manifests[SHA_FOUR] = _manifest(attempts=4)
    transport.add_run(5004, SHA_FOUR, conclusion="success")
    passed = _publisher(transport).reconcile(5004, 1)

    assert failed.consecutive_code_failures == 2
    assert passed.status == "pass"
    assert passed.action == "done_candidate"
    assert passed.consecutive_code_failures == 0
    assert passed.infrastructure_failures == 0
    assert transport.issues_data[0]["state"] == "closed"


def test_registered_layer_b_ticket_is_discovered_without_done_claim() -> None:
    transport = FakeTransport()
    transport.add_pull(ROOT_PR, SHA_ONE)
    transport.add_pull(201, SHA_TWO)
    transport.branch_tip_sha = SHA_TWO
    transport.manifests[SHA_ONE] = _manifest_with_layer_b_ticket()
    transport.manifests[SHA_TWO] = _manifest_with_layer_b_ticket()
    transport.add_run(5005, SHA_TWO, conclusion="success")

    result = _publisher(transport).reconcile(5005, 1)

    assert result.status == "pass"
    assert result.source_pr_number == 201
    assert result.action == "layer_b_required"
    assert result.evidence["ticket_id"] == "W11-011"
    assert result.evidence["layer_b_required"] is True
    assert len(transport.comments[201]) == 1
    assert transport.issues_data == []


def test_source_change_after_verification_blocks_publication() -> None:
    transport = _scenario()
    run_reads = 0

    def mutate_before_final_run_read(method: str, path: str) -> None:
        nonlocal run_reads
        if method == "GET" and path.endswith("/actions/runs/5001/attempts/1"):
            run_reads += 1
            if run_reads == 3:
                transport.runs[5001]["html_url"] += "/changed"

    transport.on_request = mutate_before_final_run_read

    with pytest.raises(PublisherError, match="source run changed before publication"):
        _publisher(transport).reconcile(5001, 1)
    assert transport.comments[ROOT_PR] == []
    assert transport.issues_data == []


def test_third_and_fourth_failures_transition_one_repair_projection() -> None:
    transport = FakeTransport()
    transport.add_pull(ROOT_PR, SHA_ONE)
    transport.add_pull(REPAIR_PR, SHA_TWO)
    transport.add_pull(THIRD_PR, SHA_THREE)
    transport.add_pull(FOURTH_PR, SHA_FOUR)
    transport.manifests[SHA_THREE] = _manifest(attempts=3)
    transport.manifests[SHA_FOUR] = _manifest(attempts=4)
    for run_id, sha in ((5001, SHA_ONE), (5002, SHA_TWO), (5003, SHA_THREE)):
        transport.add_run(
            run_id,
            sha,
            conclusion="failure",
            failed_job="layer-a / windows-unit",
            failed_step="Run Windows hardware-free tests",
        )
    transport.branch_tip_sha = SHA_THREE

    third = _publisher(transport).reconcile(5003, 1)

    assert third.status == "failure"
    assert third.action == "arbitration_requested"
    assert third.consecutive_code_failures == 3
    assert third.created_repair_issue is True
    assert transport.issues_data[0]["state"] == "open"
    assert transport.issues_data[0]["labels"] == ["type:repair", "status:blocked"]
    issue_number = transport.issues_data[0]["number"]
    post_issue_count = sum(
        method == "POST" and path.endswith("/issues")
        for method, path, _, _ in transport.requests
    )

    transport.branch_tip_sha = SHA_FOUR
    transport.add_run(
        5004,
        SHA_FOUR,
        conclusion="failure",
        failed_job="layer-a / windows-unit",
        failed_step="Run Windows hardware-free tests",
    )
    fourth = _publisher(transport).reconcile(5004, 1)

    assert fourth.status == "failure"
    assert fourth.action == "needs_human"
    assert fourth.consecutive_code_failures == 4
    assert fourth.created_repair_issue is False
    assert fourth.repair_issue_number == issue_number
    assert transport.issues_data[0]["state"] == "open"
    assert transport.issues_data[0]["labels"] == ["type:repair", "status:blocked", "needs-human"]
    assert sum(
        method == "POST" and path.endswith("/issues")
        for method, path, _, _ in transport.requests
    ) == post_issue_count == 1


def test_stale_pass_after_newer_registered_failure_cannot_close_repair() -> None:
    transport = FakeTransport()
    transport.add_pull(ROOT_PR, SHA_ONE)
    transport.add_pull(REPAIR_PR, SHA_TWO)
    transport.branch_tip_sha = SHA_TWO
    transport.manifests[SHA_ONE] = _manifest(attempts=1)
    transport.manifests[SHA_TWO] = _manifest(attempts=2)
    transport.add_run(5001, SHA_ONE, conclusion="success")
    transport.add_run(5002, SHA_TWO, conclusion="failure", failed_job="layer-a / windows-unit")
    lineage = lineage_key(REPOSITORY, "W11-010", ROOT_PR)
    transport.issues_data.append(
        {
            "number": 900,
            "body": _repair_issue_body(lineage),
            "state": "open",
            "labels": ["type:repair", "status:ready"],
            "user": {"type": "Bot", "login": "github-actions[bot]"},
        }
    )

    publisher = _publisher(transport)
    newer = publisher.reconcile(5002, 1)
    older = publisher.reconcile(5001, 1)

    assert newer.status == "failure"
    assert older.action == "historical_pass"
    assert transport.issues_data[0]["state"] == "open"
    assert len(transport.comments[ROOT_PR]) == 1


def test_malformed_repair_marker_fails_closed() -> None:
    transport = _scenario(conclusion="failure", failed_job="layer-a / windows-unit")
    lineage = lineage_key(REPOSITORY, "W11-010", ROOT_PR)
    transport.issues_data.append(
        {
            "number": 900,
            "body": f"{REPAIR_MARKER}\n```json\n{{\"repository\":\"{REPOSITORY}\"}}\n```\n",
            "state": "open",
            "user": {"type": "Bot", "login": "github-actions[bot]"},
        }
    )

    with pytest.raises(PublisherError, match="repair projection"):
        _publisher(transport).reconcile(5001, 1)


def test_wrong_check_provider_fails_closed() -> None:
    transport = _scenario()
    transport.suites[SHA_ONE][0]["app"] = {"slug": "untrusted-app"}

    with pytest.raises(PublisherError, match="check suite"):
        _publisher(transport).reconcile(5001, 1)


def test_wrong_workflow_provenance_fails_closed() -> None:
    transport = _scenario()
    transport.runs[5001]["workflow_id"] = WORKFLOW_ID + 1

    with pytest.raises(PublisherError, match="different workflow"):
        _publisher(transport).reconcile(5001, 1)


def test_wrong_repository_provenance_fails_closed() -> None:
    transport = _scenario()
    transport.repository_full_name = "untrusted/example"

    with pytest.raises(PublisherError, match="repository identity"):
        _publisher(transport).reconcile(5001, 1)


def test_partial_github_api_failure_is_not_attributed_to_code() -> None:
    transport = _scenario()
    transport.failures[
        ("GET", f"/repos/{REPOSITORY}/actions/runs/5001/attempts/1/jobs")
    ] = (503, {"message": "temporary GitHub outage"})

    with pytest.raises(PublisherError, match="HTTP 503") as failure:
        _publisher(transport).reconcile(5001, 1)
    assert failure.value.category == "infrastructure"
    assert not transport.issues_data


def test_edited_pass_marker_fails_closed() -> None:
    transport = _scenario()
    publisher = _publisher(transport)
    publisher.reconcile(5001, 1)
    comment = transport.comments[ROOT_PR][0]
    marker = re.search(r"```json\n(.*?)\n```", comment["body"], flags=re.DOTALL)
    assert marker is not None
    payload = json.loads(marker.group(1))
    payload["source_sha"] = SHA_TWO
    comment["body"] = (
        comment["body"][: marker.start(1)]
        + json.dumps(payload, sort_keys=True, separators=(",", ":"))
        + comment["body"][marker.end(1) :]
    )

    with pytest.raises(PublisherError, match="PASS projection"):
        publisher.reconcile(5001, 1)


def test_jobs_pagination_is_consumed_before_pass_publication() -> None:
    transport = _scenario()
    filler = [
        {"id": 70000 + index, "name": f"optional-{index}", "conclusion": "success", "steps": []}
        for index in range(97)
    ]
    transport.jobs[5001] = filler + transport.jobs[5001]

    result = _publisher(transport).reconcile(5001, 1)

    assert result.status == "pass"
    assert any(
        method == "GET"
        and path.endswith("/actions/runs/5001/attempts/1/jobs")
        and params
        and params.get("page") == 2
        for method, path, params, _ in transport.requests
    )


def test_concurrent_fake_reconciliation_keeps_one_repair_issue() -> None:
    transport = _scenario(conclusion="failure", failed_job="layer-a / windows-unit")

    def reconcile(_: int):
        return _publisher(transport).reconcile(5001, 1)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reconcile, (1, 2)))

    assert len(transport.issues_data) == 1
    assert sorted(result.created_repair_issue for result in results) == [False, True]
