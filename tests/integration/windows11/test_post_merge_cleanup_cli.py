from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stderr
from unittest.mock import patch

import pytest

try:
    from . import test_post_merge_cleanup as cleanup
except ImportError:
    import test_post_merge_cleanup as cleanup

from tools import post_merge_publisher as publisher_module
from tools.post_merge_publisher import CANONICAL_REPOSITORY, FINALIZER_JOB, _cli


SOURCE_RUN = cleanup.SOURCE_RUN


def _run_cli(
    fake: cleanup.FakeTransport,
    *,
    event_name: str,
    diagnostic_aware: bool,
    finalizer_run_number: int,
) -> tuple[int, dict[str, object]]:
    environment = {
        "GITHUB_REPOSITORY": CANONICAL_REPOSITORY,
        "GITHUB_EVENT_NAME": event_name,
        "GITHUB_REF": "refs/heads/develop",
        "GITHUB_RUN_ID": "37281697373",
        "GITHUB_RUN_NUMBER": str(finalizer_run_number),
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_JOB": FINALIZER_JOB,
        "POST_MERGE_RUN_ID": str(SOURCE_RUN),
        "POST_MERGE_RUN_ATTEMPT": "1",
    }
    argv = ["post_merge_publisher.py"]
    if diagnostic_aware:
        argv.append("--diagnostic-aware")
    stderr = io.StringIO()

    def transport_factory(token: str) -> cleanup.FakeTransport:
        assert token == ""
        return fake

    with (
        patch.dict(os.environ, environment, clear=True),
        patch.object(publisher_module, "UrllibTransport", transport_factory),
        patch.object(sys, "argv", argv),
        redirect_stderr(stderr),
    ):
        result = _cli()
    payload = json.loads(stderr.getvalue())
    assert isinstance(payload, dict)
    return result, payload


def _assert_pending_cli_result(
    fake: cleanup.FakeTransport,
    *,
    event_name: str,
    diagnostic_aware: bool,
    finalizer_run_number: int,
) -> None:
    result, payload = _run_cli(
        fake,
        event_name=event_name,
        diagnostic_aware=diagnostic_aware,
        finalizer_run_number=finalizer_run_number,
    )
    assert result == 1
    assert payload["status"] == "blocked"
    assert payload["category"] == "pending-authorization"
    assert "zero production POST/PATCH/DELETE" in str(payload["error"])
    assert all(method == "GET" for method, _, _, _ in fake.calls)


@pytest.mark.parametrize(
    ("event_name", "diagnostic_aware", "finalizer_run_number"),
    [
        ("workflow_run", False, 5),
        ("workflow_run", True, 5),
        ("workflow_run", False, 6),
        ("workflow_run", True, 6),
        ("workflow_run", False, 7),
        ("workflow_run", True, 7),
        ("workflow_dispatch", False, 5),
        ("workflow_dispatch", True, 6),
    ],
    ids=(
        "workflow-run-normal-run5",
        "workflow-run-aware-run5",
        "workflow-run-normal-run6",
        "workflow-run-aware-run6",
        "workflow-run-normal-run7",
        "workflow-run-aware-run7",
        "dispatch-normal-run5",
        "dispatch-aware-run6",
    ),
)
def test_cli_pause_guard_is_pending_and_get_only_for_all_delivery_modes(
    event_name: str,
    diagnostic_aware: bool,
    finalizer_run_number: int,
) -> None:
    fake = cleanup.FakeTransport()
    _assert_pending_cli_result(
        fake,
        event_name=event_name,
        diagnostic_aware=diagnostic_aware,
        finalizer_run_number=finalizer_run_number,
    )


@pytest.mark.parametrize(
    ("event_name", "diagnostic_aware"),
    [
        ("workflow_run", False),
        ("workflow_run", True),
        ("workflow_dispatch", False),
        ("workflow_dispatch", True),
    ],
    ids=(
        "workflow-run-normal",
        "workflow-run-aware",
        "dispatch-normal",
        "dispatch-aware",
    ),
)
def test_cli_missing_pause_is_nonzero_pending_and_get_only(
    event_name: str,
    diagnostic_aware: bool,
) -> None:
    fake = cleanup.FakeTransport(manifest="schema_version: 6\n")
    result, payload = _run_cli(
        fake,
        event_name=event_name,
        diagnostic_aware=diagnostic_aware,
        finalizer_run_number=6,
    )

    assert result == 1
    assert payload["status"] == "blocked"
    assert payload["category"] == "pending-authorization"
    assert "pause is missing" in str(payload["error"])
    assert all(method == "GET" for method, _, _, _ in fake.calls)


@pytest.mark.parametrize(
    "manifest",
    [
        cleanup._pause_manifest().replace(
            "  state: blocked\n",
            "  state: blocked\n  state: blocked\n",
        ),
        cleanup._pause_manifest().replace(
            "  historical_evidence:\n",
            "  malformed_evidence:\n",
        ),
        cleanup._pause_manifest() + "\nproduction_publication_pause:\n",
    ],
    ids=("duplicate-state", "malformed-child", "duplicate-pause"),
)
def test_cli_malformed_pause_is_nonzero_and_get_only(manifest: str) -> None:
    fake = cleanup.FakeTransport(manifest=manifest)
    result, payload = _run_cli(
        fake,
        event_name="workflow_run",
        diagnostic_aware=True,
        finalizer_run_number=6,
    )

    assert result == 1
    assert payload["status"] == "blocked"
    assert payload["category"] == "blocked"
    assert all(method == "GET" for method, _, _, _ in fake.calls)


@pytest.mark.parametrize(
    "registration_name",
    [
        "paired_comment_diagnostic_registration",
        "pr_comment_authorization_continuation_registration",
    ],
    ids=("paired-registration", "continuation-registration"),
)
def test_cli_old_registration_and_marker_evidence_cannot_rearm(
    registration_name: str,
) -> None:
    manifest = (
        f"{registration_name}:\n"
        "  version: 1\n"
        "  pr_number: 13\n"
        + cleanup._pause_manifest()
    )
    fake = cleanup.FakeTransport(manifest=manifest)
    assert "pr13_comment_id: 5990563780" in fake.manifest

    result, payload = _run_cli(
        fake,
        event_name="workflow_run",
        diagnostic_aware=True,
        finalizer_run_number=6,
    )

    assert result == 1
    assert payload["status"] == "blocked"
    assert payload["category"] == "blocked"
    assert "retired" in str(payload["error"])
    assert all(method == "GET" for method, _, _, _ in fake.calls)

