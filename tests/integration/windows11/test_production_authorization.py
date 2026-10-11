"""Production activation contract; all GitHub identities below are simulated fixtures."""
from __future__ import annotations

import copy
import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

try:
    from . import test_post_merge_publisher as base
    from . import test_post_merge_cleanup as cleanup
except ImportError:
    import test_post_merge_publisher as base
    import test_post_merge_cleanup as cleanup

from tools import post_merge_publisher as module


def _writes(fake):
    return [call for call in fake.requests if call[0] != "GET"]


def _blocked(manifest):
    return manifest.replace("  state: enabled\n", "  state: blocked\n").replace(
        "  reason: explicit-user-authorization\n", "  reason: pending-authorization\n"
    ).replace("  durable_permission: approved\n", "  durable_permission: awaiting_explicit_authorization\n")


def _cli(fake, event="workflow_dispatch", run=5003, attempt=1, aware=False, simulation=None):
    fake.finalizer["event"] = event
    env = {
        "GITHUB_REPOSITORY": base.REPOSITORY, "GITHUB_TOKEN": "test-actions-token",
        "GITHUB_EVENT_NAME": event, "GITHUB_REF": "refs/heads/develop",
        "GITHUB_RUN_ID": "6001", "GITHUB_RUN_NUMBER": "10", "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_JOB": "finalize", "GITHUB_SERVER_URL": "https://github.com",
        "POST_MERGE_RUN_ID": str(run) if run else "",
        "POST_MERGE_RUN_ATTEMPT": str(attempt) if run else "",
    }
    argv = ["post_merge_publisher.py"] + (["--diagnostic-aware"] if aware else [])
    if simulation:
        argv += ["--simulation-case", simulation]
    stdout, stderr = io.StringIO(), io.StringIO()
    with patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", argv), patch.object(
        module, "UrllibTransport", lambda token: fake
    ), redirect_stdout(stdout), redirect_stderr(stderr):
        result = module._cli()
    return result, json.loads(stderr.getvalue() or stdout.getvalue())


def _results(payload):
    return payload.get("results", [payload])


def _lineage():
    fake = base.FakeTransport()
    for index, (number, sha) in enumerate(zip(
        (13, 15, 21), (base.SHA_ONE, base.SHA_TWO, base.SHA_THREE)
    ), 1):
        fake.add_pull(number, sha)
        fake.add_run(5000 + index, sha)
        manifest = base._manifest(attempts=index)
        # 真實 PR13/15 的舊 source manifest 不含新的 authorization。
        fake.manifests[sha] = manifest if index == 3 else "tickets:\n" + manifest.split("tickets:\n", 1)[1]
    fake.branch_tip_sha = base.SHA_THREE
    fake.issues_data.append({
        "number": 14, "id": 5689928254, "node_id": "I_kwDOT8W6Tc8AAAABUyVqPg",
        "user": {"type": "User", "login": "Wells-Huang", "id": 34418952},
        "state": "open", "comments": 0, "body": "Simulated historical diagnostic only; immutable.",
        "labels": ["type:repair", "status:ready"],
        "html_url": f"https://github.com/{base.REPOSITORY}/issues/14",
    })
    return fake


def test_current_manifest_enables_only_authorized_scope_and_preserves_history():
    manifest = (Path(__file__).resolve().parents[3] / module.DEFAULT_MANIFEST_PATH).read_text(encoding="utf-8")
    assert module.parse_production_authorization(manifest) is True
    assert module.parse_production_publication_pause(manifest) is None
    assert "historical_production_publication_pause:" in manifest
    assert "37462042019" in manifest
    registration = module.parse_registration(manifest, "W11-010")
    assert registration.attempts[:2] == (
        module.RegistrationAttempt(13, None, None), module.RegistrationAttempt(15, 13, 14),
    )
    assert all(value.pr_number not in {16, 18, 19, 20} for value in registration.attempts)


@pytest.mark.parametrize("event", ["workflow_run", "workflow_dispatch"])
@pytest.mark.parametrize("aware", [False, True])
def test_enabled_actual_cli_reconciles_historical_manifests_and_new_registered_pr(event, aware):
    fake = _lineage()
    anchor = copy.deepcopy(fake.issues_data[0])
    result, payload = _cli(fake, event, aware=aware)
    assert result == 0
    values = _results(payload)
    assert [value["action"] for value in values] == ["historical_pass", "historical_pass", "done_candidate"]
    assert values[-1]["source_sha"] == base.SHA_THREE
    assert values[-1]["repair_issue_number"] is None
    assert len(fake.comments[21]) == 1
    assert '"historical":true' in fake.comments[13][0]["body"]
    assert fake.issues_data == [anchor]
    assert not any("/issues/14" in path for _, path, _, _ in _writes(fake))


def test_production_failure_rerun_and_pass_touch_only_new_bot_issue_and_preserve_labels():
    fake = _lineage()
    anchor = copy.deepcopy(fake.issues_data[0])
    fake.add_run(5003, base.SHA_THREE, conclusion="failure", failed_job="layer-a / windows-unit")
    first_code, first = _cli(fake)
    failure = _results(first)[-1]
    assert first_code == 0 and failure["status"] == "failure"
    number = failure["repair_issue_number"]
    assert number != 14 and failure["consecutive_code_failures"] == 1
    production = next(issue for issue in fake.issues_data if issue["number"] == number)
    production["labels"].append("maintainer-note")
    second_code, second = _cli(fake)
    assert second_code == 0
    assert _results(second)[-1]["repair_issue_number"] == number
    assert _results(second)[-1]["created_repair_issue"] is False
    assert "maintainer-note" in production["labels"]
    # 歷史 PASS delivery 仍重建最新 failure，不能關閉 production repair。
    _, historical = _cli(fake, run=5001)
    assert production["state"] == "open"
    assert _results(historical)[-1]["status"] == "failure"
    fake.add_run(5003, base.SHA_THREE, attempt=2)
    passed_code, passed = _cli(fake, attempt=2)
    assert passed_code == 0 and _results(passed)[-1]["action"] == "done_candidate"
    assert production["state"] == "closed"
    assert set(production["labels"]) == {"type:repair", "status:done", "maintainer-note"}
    assert fake.issues_data[0] == anchor
    assert len(fake.issues_data) == 2
    assert sum(method == "POST" and path.endswith("/issues") for method, path, _, _ in _writes(fake)) == 1


@pytest.mark.parametrize("entry", ["cli", "all", "single", "private"])
@pytest.mark.parametrize("control", ["missing", "blocked", "pause"])
def test_every_production_entry_is_zero_write_without_authorization(entry, control):
    fake = base._scenario()
    fake.manifests[base.SHA_ONE] = {
        "missing": "tickets:\n", "blocked": _blocked(base._manifest()), "pause": cleanup._pause_manifest(),
    }[control]
    publisher = base._publisher(fake)
    if entry == "cli":
        code, payload = _cli(fake, run=5001)
        assert code == 1 and payload["category"] == "pending-authorization"
    else:
        with pytest.raises(module.PublisherError, match="pending-authorization"):
            if entry == "all":
                publisher.reconcile_all(5001, 1)
            elif entry == "single":
                publisher.reconcile(5001, 1)
            else:
                publisher._close_repair_if_current_locked("unverified", None)
    assert _writes(fake) == []


@pytest.mark.parametrize("key", list(module.AUTHORIZATION_FIELDS))
@pytest.mark.parametrize("change", ["missing", "duplicate", "wrong_type"])
def test_authorization_rejects_each_missing_duplicate_or_wrong_type_field_before_write(key, change):
    fake = base._scenario()
    line = f"  {key}: {module.AUTHORIZATION_FIELDS[key]}\n"
    replacement = {"missing": "", "duplicate": line * 2, "wrong_type": f"  {key}: null\n"}[change]
    fake.manifests[base.SHA_ONE] = fake.manifests[base.SHA_ONE].replace(line, replacement)
    code, payload = _cli(fake, run=5001)
    assert code == 1 and payload["status"] == "blocked"
    assert _writes(fake) == []


@pytest.mark.parametrize("suffix", [
    "production_publication_authorization:\n", cleanup._pause_manifest(),
    "paired_comment_diagnostic_registration: {}\n",
    "pr_comment_authorization_continuation_registration: null\n",
    '"paired_comment_diagnostic_registration":\n  version: 1\n',
])
def test_duplicate_conflicting_or_malformed_retired_controls_are_zero_write(suffix):
    fake = base._scenario()
    fake.manifests[base.SHA_ONE] += suffix
    code, payload = _cli(fake, run=5001)
    assert code == 1 and payload["status"] == "blocked"
    assert _writes(fake) == []


@pytest.mark.parametrize("replacement", [
    "  unknown: true\n", "# comment cannot terminate a mapping\n  unknown: true\n",
    " state: enabled\n", "  state: enabled\n",
])
def test_authorization_unknown_or_misindented_tail_cannot_hide_after_valid_fields(replacement):
    fake = base._scenario()
    fake.manifests[base.SHA_ONE] = fake.manifests[base.SHA_ONE].replace(
        "historical_diagnostic_anchors:\n", replacement + "historical_diagnostic_anchors:\n"
    )
    assert _cli(fake, run=5001)[0] == 1
    assert _writes(fake) == []


@pytest.mark.parametrize("old,new", [
    ("issue_number: 14", "issue_number: 15"),
    ("publisher_mutation: forbidden", "publisher_mutation: approved"),
    ("historical_diagnostic_anchors:", "missing_anchors:"),
])
def test_historical_anchor_configuration_is_strict(old, new):
    fake = base._scenario()
    fake.manifests[base.SHA_ONE] = fake.manifests[base.SHA_ONE].replace(old, new)
    assert _cli(fake, run=5001)[0] == 1
    assert _writes(fake) == []


@pytest.mark.parametrize("stage", ["before_comment", "after_issue"])
def test_revocation_is_checked_again_at_each_mutation_stage(stage):
    fake = base._scenario(conclusion="failure" if stage == "after_issue" else "success", failed_job="layer-a / windows-unit" if stage == "after_issue" else None)
    def revoke(method, path):
        should_revoke = path.endswith("/issues/13/comments") if stage == "before_comment" else bool(fake.issues_data)
        if method == "GET" and should_revoke:
            fake.manifests[base.SHA_ONE] = _blocked(fake.manifests[base.SHA_ONE])
    fake.on_request = revoke
    code, payload = _cli(fake, run=5001)
    assert code == 1 and payload["category"] == "pending-authorization"
    assert len(_writes(fake)) == (1 if stage == "after_issue" else 0)


def test_unregistered_activation_delivery_cannot_borrow_pr15_completion():
    fake = _lineage()
    # 模擬漏登記 activation PR21，current protected manifest 僅有 PR13/15。
    fake.manifests[base.SHA_THREE] = base._manifest(attempts=2)
    code, payload = _cli(fake)
    assert code == 0
    values = _results(payload)
    assert values[0]["action"] == "non_production_delivery"
    assert all(value["action"] != "done_candidate" for value in values)
    assert fake.comments[21] == []
    assert len(fake.comments[15]) == 1


def test_unregistered_cleanup_delivery_wakes_latest_registered_failure():
    fake = _lineage()
    fake.add_run(5003, base.SHA_THREE, conclusion="failure", failed_job="layer-a / windows-unit")
    fake.add_pull(20, base.SHA_FOUR)
    fake.add_run(5004, base.SHA_FOUR)
    fake.manifests[base.SHA_FOUR] = base._manifest(attempts=3)
    fake.branch_tip_sha = base.SHA_FOUR
    code, payload = _cli(fake, event="workflow_run", run=5004)
    assert code == 0
    assert _results(payload)[0]["status"] == "unregistered"
    assert _results(payload)[-1]["status"] == "failure"
    assert fake.comments[20] == []


def test_source_registration_prefix_drift_blocks_all_before_mutation():
    fake = _lineage()
    fake.manifests[base.SHA_TWO] = fake.manifests[base.SHA_TWO].replace("repair_issue_number: 14", "repair_issue_number: 900")
    code, payload = _cli(fake)
    assert code == 1 and "extension" in payload["error"]
    assert _writes(fake) == []


def test_cross_run_order_uses_trusted_start_time_not_attempt_or_run_id():
    fake = base._scenario()
    for attempt in range(2, 6):
        fake.add_run(5001, base.SHA_ONE, attempt=attempt)
    # 較小 run id，較新 start time，attempt 1 必須壓過舊 attempt 5。
    fake.add_run(4999, base.SHA_ONE, conclusion="failure", failed_job="layer-a / windows-unit")
    fake.runs[4999]["run_started_at"] = "2026-12-01T00:00:00Z"
    failed = base._publisher(fake).reconcile(4999, 1)
    delayed = base._publisher(fake).reconcile(5001, 5)
    assert failed.status == "failure"
    assert delayed.action == "no_mutation_stale_result"
    assert fake.issues_data[0]["state"] == "open"
    assert not fake.comments[13]


def test_full_pagination_for_runs_issues_comments_and_suites():
    fake = _lineage()
    for index in range(100):
        fake.issues_data.insert(0, {"number": 10000 + index, "body": "unrelated"})
        fake.comments[21].append({"id": index, "body": "unrelated"})
    actual_runs = fake.runs
    fake.runs = {index: {"id": index, "head_sha": "9" * 40} for index in range(100)} | actual_runs
    fake.suites[base.SHA_THREE] = [{"id": index} for index in range(100)] + fake.suites[base.SHA_THREE]
    code, _ = _cli(fake)
    assert code == 0
    for suffix in ("/runs", "/issues", "/issues/21/comments", "/check-suites"):
        assert any(method == "GET" and path.endswith(suffix) and params and params.get("page") == 2 for method, path, params, _ in fake.requests)


@pytest.mark.parametrize("kind", ["comment", "issue"])
@pytest.mark.parametrize("response", ["uncertain_written", "uncertain_missing", "success_missing", "forbidden"])
def test_publication_requires_readback_and_never_repeats_uncertain_post(kind, response):
    fake = base._scenario(conclusion="failure" if kind == "issue" else "success", failed_job="layer-a / windows-unit" if kind == "issue" else None)
    original = fake.request
    def request(method, path, **kwargs):
        if method == "POST":
            if response == "forbidden":
                return module.HttpResponse(403, {"message": "Resource not accessible by integration"})
            value = original(method, path, **kwargs)
            if response.endswith("missing"):
                fake.issues_data.clear()
                fake.comments[13].clear()
            if response.startswith("uncertain"):
                raise module.PublisherError("transport timeout", category="infrastructure")
            return value
        return original(method, path, **kwargs)
    fake.request = request
    code, _ = _cli(fake, run=5001)
    assert code == (0 if response == "uncertain_written" else 1)
    assert sum(method == "POST" for method, _, _, _ in _writes(fake)) <= 1


def test_production_marker_on_human_issue_is_not_silently_ignored():
    fake = base._scenario(conclusion="failure", failed_job="layer-a / windows-unit")
    base._publisher(fake).reconcile(5001, 1)
    fake.issues_data[0]["user"] = {"type": "User", "login": "maintainer"}
    fake.requests.clear()
    assert _cli(fake, run=5001)[0] == 1
    assert _writes(fake) == []


def test_blocked_reducer_result_is_nonzero_and_is_not_acceptance_pass():
    fake = base._scenario(conclusion="failure", failed_job="layer-a / windows-unit", runner_failure=True)
    code, payload = _cli(fake, run=5001)
    assert code == 1 and payload["action"] == "infrastructure_blocked"
    assert payload["consecutive_code_failures"] == 0
    assert _writes(fake) == []


def test_manual_without_source_reconciles_all_and_simulation_stays_isolated():
    fake = _lineage()
    assert _cli(fake, run=None)[0] == 0
    before = copy.deepcopy((fake.comments, fake.issues_data, fake.manifests))
    values = [_cli(fake, run=None, simulation=case) for case in ("pass", "fail", "rerun")]
    assert all(code == 0 for code, _ in values)
    assert len({value["issue_number"] for _, value in values}) == 1
    assert values[-1][1]["consecutive_code_failures"] == 1
    assert (fake.comments, fake.issues_data[:1], fake.manifests) == before


@pytest.mark.parametrize("field,value", [
    ("workflow_id", 999), ("path", ".github/workflows/other.yml"),
    ("repository", None), ("event", "pull_request"), ("head_branch", "other"),
    ("run_attempt", 2), ("status", "cancelled"),
])
def test_enabled_finalizer_identity_is_revalidated_before_any_write(field, value):
    fake = base._scenario()
    fake.finalizer[field] = value
    with pytest.raises(module.PublisherError):
        base._publisher(fake).reconcile(5001, 1)
    assert _writes(fake) == []


def test_enabled_dispatch_wrong_ref_is_zero_write():
    fake = base._scenario()
    publisher = base._publisher(fake)
    from dataclasses import replace
    publisher.config = replace(publisher.config, ref="refs/heads/untrusted")
    with pytest.raises(module.PublisherError, match="protected develop"):
        publisher.reconcile_all()
    assert _writes(fake) == []


@pytest.mark.parametrize("field,value", [
    ("path", ".github/workflows/ci.yml"), ("event", "pull_request"),
    ("head_branch", "topic"), ("status", "in_progress"), ("run_started_at", ""),
])
def test_enabled_producer_identity_and_ordering_are_verified(field, value):
    fake = base._scenario()
    fake.runs[5001][field] = value
    assert _cli(fake, run=5001)[0] == 1
    assert _writes(fake) == []


def test_non_ancestor_source_is_zero_write():
    fake = _lineage()
    original = fake.request
    def request(method, path, **kwargs):
        if "/compare/" in path:
            return module.HttpResponse(200, {"status": "diverged"})
        return original(method, path, **kwargs)
    fake.request = request
    assert _cli(fake)[0] == 1
    assert _writes(fake) == []


def test_production_issue_target_drift_is_detected_between_list_and_get():
    fake = base._scenario(conclusion="failure", failed_job="layer-a / windows-unit")
    base._publisher(fake).reconcile(5001, 1)
    fake.requests.clear()
    def drift(method, path):
        if method == "GET" and path.endswith("/issues/900"):
            fake.issues_data[0]["body"] += "\nexternal edit"
    fake.on_request = drift
    code, payload = _cli(fake, run=5001)
    assert code == 1 and "target changed" in payload["error"]
    assert _writes(fake) == []


def test_conflicting_production_candidates_fail_without_mutation():
    fake = base._scenario(conclusion="failure", failed_job="layer-a / windows-unit")
    base._publisher(fake).reconcile(5001, 1)
    other = copy.deepcopy(fake.issues_data[0])
    other.update(number=901, html_url=f"https://github.com/{base.REPOSITORY}/issues/901")
    fake.issues_data.append(other)
    fake.requests.clear()
    code, payload = _cli(fake, run=5001)
    assert code == 1 and "multiple repair issues" in payload["error"]
    assert _writes(fake) == []


def test_partial_labels_failure_reruns_same_issue_without_new_retry():
    fake = base._scenario(conclusion="failure", failed_job="layer-a / windows-unit")
    path = f"/repos/{base.REPOSITORY}/issues/900/labels"
    fake.failures[("PUT", path)] = (403, {"message": "Resource not accessible by integration"})
    assert _cli(fake, run=5001)[0] == 1
    assert len(fake.issues_data) == 1
    del fake.failures[("PUT", path)]
    code, payload = _cli(fake, run=5001)
    assert code == 0 and payload["repair_issue_number"] == 900
    assert payload["created_repair_issue"] is False and payload["consecutive_code_failures"] == 1
    assert len(fake.issues_data) == 1


def test_revocation_after_pass_comment_leaves_existing_repair_open():
    fake = base._scenario(conclusion="failure", failed_job="layer-a / windows-unit")
    base._publisher(fake).reconcile(5001, 1)
    fake.add_run(5001, base.SHA_ONE, attempt=2)
    def revoke(method, path):
        if method == "GET" and fake.comments[13]:
            fake.manifests[base.SHA_ONE] = _blocked(fake.manifests[base.SHA_ONE])
    fake.on_request = revoke
    assert _cli(fake, run=5001, attempt=2)[0] == 1
    assert fake.issues_data[0]["state"] == "open"


def test_registered_missing_producer_history_is_not_a_pass_or_zero_retry_reset():
    fake = _lineage()
    del fake.runs[5001]
    code, payload = _cli(fake)
    assert code == 1 and "missing producer history" in payload["error"]
    assert _writes(fake) == []


@pytest.mark.parametrize("first_pass", [True, False])
def test_rerun_with_shared_mutable_suite_preserves_attempt_specific_history(first_pass):
    fake = base._scenario(
        conclusion="success" if first_pass else "failure",
        failed_job=None if first_pass else "layer-a / windows-unit",
    )
    if not first_pass:
        base._publisher(fake).reconcile(5001, 1)
    suite_id = fake.runs[5001]["check_suite_id"]
    fake.add_run(5001, base.SHA_ONE, attempt=2,
                 conclusion="failure" if first_pass else "success",
                 failed_job="layer-a / windows-unit" if first_pass else None)
    # 同 run 的多次 attempts 使用同一 suite ID；suite summary 反映最新結果。
    fake.runs[5001]["check_suite_id"] = suite_id
    fake.suites[base.SHA_ONE] = [dict(fake.suites[base.SHA_ONE][-1], id=suite_id)]
    code, current = _cli(fake, run=5001, attempt=2)
    assert code == 0
    latest = next(value for value in _results(current) if value["source_run_attempt"] == 2)
    assert latest["status"] == ("failure" if first_pass else "pass")
    assert fake.issues_data[0]["state"] == ("open" if first_pass else "closed")
    delayed = base._publisher(fake).reconcile(5001, 1)
    assert delayed.status == ("historical_pass" if first_pass else "historical_failure")
    assert delayed.action == "no_mutation_stale_result"
