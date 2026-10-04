"""Run one bounded, isolated GitHub Issues comment diagnostic for W11-010.

This module intentionally does not import the production post-merge publisher.
It has one fixed namespace, uses only the current job's GITHUB_TOKEN, and
keeps all writes bounded and idempotent across workflow reruns.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_ROOT = "https://api.github.com"
CANONICAL_REPOSITORY = "Wells-sideproj/obs-voice-command-windows"
DIAGNOSTIC_NAMESPACE = "w11-010-comment-probe"
ISSUE_MARKER = "<!-- w11-010-comment-probe:issue:v1 -->"
COMMENT_MARKER = "<!-- w11-010-comment-probe:comment:v1 -->"
PROBE_VERSION = 1
MAX_ISSUE_PAGES = 20
MAX_COMMENT_PAGES = 20
PAGE_SIZE = 100
VALID_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
SAFE_HTTP_MESSAGES = frozenset(
    {
        "Bad credentials",
        "Forbidden",
        "Internal Server Error",
        "Not Found",
        "Resource not accessible by integration",
        "Resource not accessible by personal access token",
        "Unprocessable Entity",
        "Validation Failed",
    }
)


class ProbeError(RuntimeError):
    """A fail-closed diagnostic error with no sensitive payload."""


OPERATION_LABELS = frozenset(
    {
        "repository_info",
        "list_issues",
        "create_issue",
        "get_issue",
        "list_comments",
        "create_comment",
        "close_issue",
        "transport",
    }
)


def _operation_label(value: Any) -> str:
    return value if isinstance(value, str) and value in OPERATION_LABELS else "transport"


class ApiError(ProbeError):
    """A sanitized HTTP API failure."""

    def __init__(self, operation: str, status: int, detail: str) -> None:
        self.operation = _operation_label(operation)
        self.status = status if isinstance(status, int) and 100 <= status <= 599 else 0
        self.message = _allowlisted_http_message(detail)
        super().__init__(
            f"github_api_http_error operation={self.operation} "
            f"HTTP {self.status}: {self.message}"
        )


class OperationError(ProbeError):
    """A sanitized non-HTTP failure associated with one API operation."""

    def __init__(self, operation: str, category: str) -> None:
        self.operation = _operation_label(operation)
        self.category = (
            category
            if category in {"probe_error", "unexpected_error"}
            else "probe_error"
        )
        super().__init__(
            f"github_api_operation_error operation={self.operation} "
            f"category={self.category}"
        )


class Transport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        ...


def _allowlisted_http_message(value: Any) -> str:
    return value if isinstance(value, str) and value in SAFE_HTTP_MESSAGES else "HTTP error"


def _error_label(exc: Exception) -> str:
    if isinstance(exc, (ApiError, OperationError)):
        return str(exc)
    if isinstance(exc, ProbeError):
        return "probe_error"
    return "unexpected_error"


def _safe_http_detail(body: bytes, status: int) -> str:
    try:
        decoded = json.loads(body.decode("utf-8", errors="replace"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "HTTP error"
    if isinstance(decoded, dict):
        message = decoded.get("message")
        if isinstance(message, str) and message in SAFE_HTTP_MESSAGES:
            return message
    return "HTTP error"


class UrllibTransport:
    """Small standard-library transport that never exposes Authorization."""

    def __init__(self, token: str, *, timeout: float = 30.0) -> None:
        self._token = token
        self._timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        data = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        request = Request(
            f"{API_ROOT}{path}",
            data=data,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
                status = getattr(response, "status", 200)
        except HTTPError as exc:
            raise ApiError(
                "transport",
                exc.code,
                _safe_http_detail(exc.read(4096), exc.code),
            ) from None
        except URLError as exc:
            del exc
            raise ProbeError("github_api_network_error") from None
        except OSError as exc:
            del exc
            raise ProbeError("github_api_transport_error") from None
        if status == 204 or not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProbeError("github_api_invalid_json") from None


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProbeError(f"{label} response was not an object")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ProbeError(f"{label} response was not a list")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProbeError(f"{label} must be a positive integer")
    return value


def _payload_from_body(body: Any, marker: str) -> Mapping[str, Any] | None:
    if not isinstance(body, str) or marker not in body:
        return None
    normalized = body.replace("\r\n", "\n")
    match = re.search(
        rf"{re.escape(marker)}\n```json\n(.*?)\n```",
        normalized,
        flags=re.DOTALL,
    )
    if match is None:
        raise ProbeError(f"diagnostic marker {marker} has no valid JSON block")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        raise ProbeError(f"diagnostic marker {marker} contains invalid JSON") from None
    return _require_mapping(payload, f"diagnostic marker {marker}")


def _json_body(marker: str, payload: Mapping[str, Any], description: str) -> str:
    return "\n".join(
        [
            marker,
            "```json",
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            "```",
            "",
            description,
        ]
    )


@dataclass(frozen=True)
class ProbeConfig:
    repository: str
    token: str
    event_name: str
    ref: str
    sha: str
    run_id: int
    run_attempt: int
    server_url: str = "https://github.com"

    @property
    def run_url(self) -> str:
        return (
            f"{self.server_url.rstrip('/')}/{self.repository}/actions/runs/"
            f"{self.run_id}"
        )


@dataclass
class _RunState:
    issue_number: int | None = None
    issue_url: str | None = None
    issue_closed: bool = False
    close_attempted: bool = False


@dataclass(frozen=True)
class ProbeResult:
    issue_number: int
    issue_url: str
    created_issue: bool
    created_comment: bool
    comment_verified: bool
    issue_closed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "success",
            "namespace": DIAGNOSTIC_NAMESPACE,
            "issue_number": self.issue_number,
            "issue_url": self.issue_url,
            "created_issue": self.created_issue,
            "comment_posted": self.created_comment,
            "comment_verified": self.comment_verified,
            "issue_closed": self.issue_closed,
            "persistent_closed_issue": True,
            "deleted": False,
        }


class GithubApi:
    def __init__(self, transport: Transport, repository: str) -> None:
        self._transport = transport
        self.repository = repository

    @property
    def _repo_path(self) -> str:
        return f"/repos/{self.repository}"

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        operation: str,
    ) -> Mapping[str, Any] | list[Any]:
        try:
            return self._transport.request(method, path, payload)
        except ApiError as exc:
            raise ApiError(operation, exc.status, exc.message) from None
        except OperationError as exc:
            raise OperationError(operation, exc.category) from None
        except ProbeError:
            raise OperationError(operation, "probe_error") from None
        except Exception:
            raise OperationError(operation, "unexpected_error") from None

    def repository_info(self) -> Mapping[str, Any]:
        return _require_mapping(
            self._request(
                "GET",
                self._repo_path,
                operation="repository_info",
            ),
            "repository",
        )

    def issues(self, page: int) -> list[Any]:
        query = urlencode({"state": "all", "per_page": PAGE_SIZE, "page": page})
        return _require_list(
            self._request(
                "GET",
                f"{self._repo_path}/issues?{query}",
                operation="list_issues",
            ),
            "issues",
        )

    def create_issue(self, title: str, body: str) -> Mapping[str, Any]:
        return _require_mapping(
            self._request(
                "POST",
                f"{self._repo_path}/issues",
                {"title": title, "body": body},
                operation="create_issue",
            ),
            "created issue",
        )

    def issue(self, number: int) -> Mapping[str, Any]:
        return _require_mapping(
            self._request(
                "GET",
                f"{self._repo_path}/issues/{number}",
                operation="get_issue",
            ),
            "issue",
        )

    def comments(self, number: int, page: int) -> list[Any]:
        query = urlencode({"per_page": PAGE_SIZE, "page": page})
        return _require_list(
            self._request(
                "GET",
                f"{self._repo_path}/issues/{number}/comments?{query}",
                operation="list_comments",
            ),
            "comments",
        )

    def create_comment(self, number: int, body: str) -> Mapping[str, Any]:
        return _require_mapping(
            self._request(
                "POST",
                f"{self._repo_path}/issues/{number}/comments",
                {"body": body},
                operation="create_comment",
            ),
            "created comment",
        )

    def close_issue(self, number: int) -> Mapping[str, Any]:
        return _require_mapping(
            self._request(
                "PATCH",
                f"{self._repo_path}/issues/{number}",
                {"state": "closed"},
                operation="close_issue",
            ),
            "closed issue",
        )


class ProbeFailure(ProbeError):
    """A probe failure with explicit cleanup evidence."""

    def __init__(
        self,
        primary_error: str,
        *,
        issue_number: int | None,
        issue_url: str | None,
        cleanup: str,
    ) -> None:
        super().__init__(primary_error)
        self.primary_error = primary_error
        self.issue_number = issue_number
        self.issue_url = issue_url
        self.cleanup = cleanup

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "failure",
            "namespace": DIAGNOSTIC_NAMESPACE,
            "error": self.primary_error,
            "cleanup": self.cleanup,
            "deleted": False,
        }
        if self.issue_number is not None:
            result["issue_number"] = self.issue_number
        if self.issue_url is not None:
            result["issue_url"] = self.issue_url
        return result


def _trusted_bot(value: Mapping[str, Any]) -> bool:
    user = value.get("user")
    if not isinstance(user, Mapping):
        return False
    return (
        user.get("type") == "Bot"
        and user.get("login") == "github-actions[bot]"
    )


def _validate_common_payload(
    payload: Mapping[str, Any],
    *,
    config: ProbeConfig,
    kind: str,
) -> None:
    if (
        payload.get("kind") != kind
        or payload.get("version") != PROBE_VERSION
        or payload.get("namespace") != DIAGNOSTIC_NAMESPACE
        or payload.get("repository") != config.repository
    ):
        raise ProbeError("diagnostic projection has an invalid protected schema")


class CommentProbe:
    """One bounded diagnostic run against one fixed public issue namespace."""

    issue_title = "[W11-010 diagnostic] isolated comment permission probe"

    def __init__(self, api: GithubApi, config: ProbeConfig) -> None:
        self.api = api
        self.config = config
        self.state = _RunState()

    def _issue_url(self, issue: Mapping[str, Any], number: int) -> str:
        value = issue.get("html_url")
        if (
            isinstance(value, str)
            and value == f"https://github.com/{self.config.repository}/issues/{number}"
        ):
            return value
        return f"https://github.com/{self.config.repository}/issues/{number}"

    def _issue_body(self) -> str:
        return _json_body(
            ISSUE_MARKER,
            {
                "kind": "w11-010-comment-probe-issue",
                "version": PROBE_VERSION,
                "namespace": DIAGNOSTIC_NAMESPACE,
                "repository": self.config.repository,
                "public": True,
                "source": {
                    "event": self.config.event_name,
                    "ref": self.config.ref,
                    "sha": self.config.sha,
                    "run_id": self.config.run_id,
                    "run_attempt": self.config.run_attempt,
                    "run_url": self.config.run_url,
                },
            },
            "This is a bounded public W11-010 comment-permission diagnostic.",
        )

    def _comment_body(self) -> str:
        return _json_body(
            COMMENT_MARKER,
            {
                "kind": "w11-010-comment-probe-comment",
                "version": PROBE_VERSION,
                "namespace": DIAGNOSTIC_NAMESPACE,
                "repository": self.config.repository,
                "diagnostic": "issue-comment-write-readback",
                "source": {
                    "event": self.config.event_name,
                    "ref": self.config.ref,
                    "sha": self.config.sha,
                    "run_id": self.config.run_id,
                    "run_attempt": self.config.run_attempt,
                },
            },
            "Diagnostic marker: issue comment write and read-back succeeded.",
        )

    def _validate_issue(self, issue: Mapping[str, Any]) -> tuple[int, str, bool]:
        if "pull_request" in issue:
            raise ProbeError("diagnostic namespace was found on a pull request")
        number = _positive_int(issue.get("number"), "diagnostic issue number")
        if not _trusted_bot(issue):
            raise ProbeError("diagnostic issue author is not a GitHub bot")
        payload = _payload_from_body(issue.get("body"), ISSUE_MARKER)
        if payload is None:
            raise ProbeError("diagnostic issue marker is missing")
        _validate_common_payload(
            payload,
            config=self.config,
            kind="w11-010-comment-probe-issue",
        )
        if payload.get("public") is not True:
            raise ProbeError("diagnostic issue is not marked public")
        state = issue.get("state")
        if state not in {"open", "closed"}:
            raise ProbeError("diagnostic issue state is invalid")
        return number, self._issue_url(issue, number), state == "closed"

    def _find_issue(self) -> tuple[Mapping[str, Any], int, str, bool] | None:
        matches: list[tuple[Mapping[str, Any], int, str, bool]] = []
        for page in range(1, MAX_ISSUE_PAGES + 1):
            items = self.api.issues(page)
            for item in items:
                issue = _require_mapping(item, "issue")
                body = issue.get("body")
                if not isinstance(body, str) or ISSUE_MARKER not in body:
                    continue
                matches.append((issue, *self._validate_issue(issue)))
            if len(items) < PAGE_SIZE:
                break
        else:
            raise ProbeError("diagnostic issue search exceeded the page bound")
        if len(matches) > 1:
            raise ProbeError("multiple diagnostic issues exist for the fixed namespace")
        return matches[0] if matches else None

    def _validate_comment(self, comment: Mapping[str, Any]) -> None:
        if not _trusted_bot(comment):
            raise ProbeError("diagnostic comment author is not a GitHub bot")
        payload = _payload_from_body(comment.get("body"), COMMENT_MARKER)
        if payload is None:
            raise ProbeError("diagnostic comment marker is missing")
        _validate_common_payload(
            payload,
            config=self.config,
            kind="w11-010-comment-probe-comment",
        )
        if payload.get("diagnostic") != "issue-comment-write-readback":
            raise ProbeError("diagnostic comment payload is invalid")

    def _find_comment(self, number: int) -> Mapping[str, Any] | None:
        matches: list[Mapping[str, Any]] = []
        for page in range(1, MAX_COMMENT_PAGES + 1):
            items = self.api.comments(number, page)
            for item in items:
                comment = _require_mapping(item, "comment")
                body = comment.get("body")
                if not isinstance(body, str) or COMMENT_MARKER not in body:
                    continue
                self._validate_comment(comment)
                matches.append(comment)
            if len(items) < PAGE_SIZE:
                break
        else:
            raise ProbeError("diagnostic comment search exceeded the page bound")
        if len(matches) > 1:
            raise ProbeError("multiple diagnostic comments exist for the fixed namespace")
        return matches[0] if matches else None

    def _close_if_needed(self) -> None:
        if self.state.issue_closed:
            return
        if self.state.issue_number is None:
            raise ProbeError("cannot close a diagnostic issue without its number")
        self.state.close_attempted = True
        response = self.api.close_issue(self.state.issue_number)
        if response.get("state") != "closed":
            raise ProbeError("close response did not report a closed issue")
        readback = self.api.issue(self.state.issue_number)
        if readback.get("state") != "closed":
            raise ProbeError("close read-back did not report a closed issue")
        self.state.issue_closed = True

    def _best_effort_close(self) -> str:
        if self.state.issue_number is None:
            return "not attempted: issue number unavailable"
        if self.state.issue_closed:
            return f"already closed issue #{self.state.issue_number}"
        if self.state.close_attempted:
            return "close failed: close request already attempted"
        self.state.close_attempted = True
        try:
            response = self.api.close_issue(self.state.issue_number)
            if response.get("state") != "closed":
                return "close failed: response did not report a closed issue"
            readback = self.api.issue(self.state.issue_number)
            if readback.get("state") != "closed":
                return "close failed: read-back did not report a closed issue"
            self.state.issue_closed = True
            return f"closed issue #{self.state.issue_number}"
        except Exception as exc:
            return f"close failed: {_error_label(exc)}"

    def run(self) -> ProbeResult:
        try:
            repository = self.api.repository_info()
            if (
                repository.get("full_name", "").lower()
                != self.config.repository.lower()
                or repository.get("private") is not False
            ):
                raise ProbeError("diagnostic requires the canonical public repository")

            found = self._find_issue()
            created_issue = False
            if found is None:
                issue = self.api.create_issue(self.issue_title, self._issue_body())
                candidate_number = issue.get("number")
                if (
                    isinstance(candidate_number, int)
                    and not isinstance(candidate_number, bool)
                    and candidate_number > 0
                ):
                    self.state.issue_number = candidate_number
                    self.state.issue_url = self._issue_url(issue, candidate_number)
                    self.state.issue_closed = issue.get("state") == "closed"
                number, issue_url, closed = self._validate_issue(issue)
                created_issue = True
            else:
                issue, number, issue_url, closed = found
            self.state.issue_number = number
            self.state.issue_url = issue_url
            self.state.issue_closed = closed

            comment = self._find_comment(number)
            created_comment = False
            if comment is None:
                self.api.create_comment(number, self._comment_body())
                created_comment = True
                comment = self._find_comment(number)
            if comment is None:
                raise ProbeError("diagnostic comment read-back did not find its marker")

            self._close_if_needed()
            return ProbeResult(
                issue_number=number,
                issue_url=issue_url,
                created_issue=created_issue,
                created_comment=created_comment,
                comment_verified=True,
                issue_closed=self.state.issue_closed,
            )
        except ProbeFailure:
            raise
        except Exception as exc:
            primary = _error_label(exc)
            raise ProbeFailure(
                primary,
                issue_number=self.state.issue_number,
                issue_url=self.state.issue_url,
                cleanup=self._best_effort_close(),
            ) from None


def run_probe(api: GithubApi, config: ProbeConfig) -> ProbeResult:
    return CommentProbe(api, config).run()


def _parse_positive_int(value: str, label: str) -> int:
    if not value.isdigit() or int(value) <= 0:
        raise ProbeError(f"{label} must be a positive integer")
    return int(value)


def config_from_environment(environ: Mapping[str, str] | None = None) -> ProbeConfig:
    values = os.environ if environ is None else environ
    repository = values.get("GITHUB_REPOSITORY", "")
    event_name = values.get("GITHUB_EVENT_NAME", "")
    ref = values.get("GITHUB_REF", "")
    token = values.get("GITHUB_TOKEN", "")
    sha = values.get("GITHUB_SHA", "")
    if event_name != "workflow_dispatch":
        raise ProbeError("diagnostic requires a workflow_dispatch event")
    if ref != "refs/heads/develop":
        raise ProbeError("diagnostic requires refs/heads/develop")
    if repository.lower() != CANONICAL_REPOSITORY.lower():
        raise ProbeError("diagnostic requires the canonical repository")
    if not token:
        raise ProbeError("GITHUB_TOKEN is required")
    if not VALID_SHA.fullmatch(sha):
        raise ProbeError("GITHUB_SHA must be a 40-character commit SHA")
    run_id = _parse_positive_int(values.get("GITHUB_RUN_ID", ""), "GITHUB_RUN_ID")
    run_attempt = _parse_positive_int(
        values.get("GITHUB_RUN_ATTEMPT", "1"),
        "GITHUB_RUN_ATTEMPT",
    )
    return ProbeConfig(
        repository=CANONICAL_REPOSITORY,
        token=token,
        event_name=event_name,
        ref=ref,
        sha=sha.lower(),
        run_id=run_id,
        run_attempt=run_attempt,
        server_url=values.get("GITHUB_SERVER_URL", "https://github.com"),
    )


def _failure_for_context(exc: Exception, token: str = "") -> dict[str, Any]:
    return {
        "status": "failure",
        "namespace": DIAGNOSTIC_NAMESPACE,
        "error": _error_label(exc),
        "cleanup": "not attempted: diagnostic context was rejected",
        "deleted": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        config = config_from_environment()
        result = run_probe(
            GithubApi(UrllibTransport(config.token), config.repository),
            config,
        )
    except ProbeFailure as exc:
        print(json.dumps(exc.as_dict(), sort_keys=True), file=sys.stderr)
        return 1
    except Exception as exc:
        token = os.environ.get("GITHUB_TOKEN", "")
        print(
            json.dumps(_failure_for_context(exc, token), sort_keys=True),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
