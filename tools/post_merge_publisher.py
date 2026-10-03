"""Trusted post-merge evidence publisher for W11-010.

The finalizer runs this module from the protected repository context.  The
GitHub transport is deliberately separate from the reducer so tests can drive
the real publisher with an HTTP-shaped fake without depending on GitHub or a
third-party package.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import threading
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


DEFAULT_WORKFLOW_PATH = ".github/workflows/post-merge.yml"
DEFAULT_MANIFEST_PATH = "docs/plans/2026-08-17-windows-11-ticket-manifest.yml"
CANONICAL_REPOSITORY = "Wells-sideproj/obs-voice-command-windows"
DEFAULT_REQUIRED_JOBS = (
    "layer-a / windows-unit",
    "layer-a / macos-regression",
    "layer-a / package",
    "required / gate",
)
# 只有這些 workflow 步驟能直接證明程式碼或測試失敗；setup、鎖定依賴、
# runner、平台 guard 與 aggregate gate 的失敗都不能單獨消耗 code retry。
CODE_ATTRIBUTABLE_STEP_NAMES = frozenset(
    {
        "Verify Windows imports stay Quartz-free",
        "Verify macOS imports and CLI",
        "Run Windows hardware-free tests",
        "Run macOS regression tests",
    }
)
AGGREGATE_JOB_NAMES = frozenset({"required / gate"})
REGISTRATION_VERSION = 1
PASS_MARKER = "<!-- w11-010-post-merge:v1 -->"
REPAIR_MARKER = "<!-- w11-010-repair:v1 -->"
SIMULATION_MARKER = "<!-- w11-010-simulation:v1 -->"
SIMULATION_NAMESPACE = "w11-010-simulation"
SIMULATION_CASES = ("pass", "fail", "rerun")
VALID_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
NULL = {"null", "~"}
MAX_API_ERROR_DETAIL_LENGTH = 1024
MAX_HTTP_ERROR_MESSAGE_LENGTH = 240
MAX_HTTP_ERROR_DOCUMENTATION_URL_LENGTH = 256
MAX_HTTP_ERROR_REQUEST_ID_LENGTH = 128
MAX_HTTP_ERROR_PERMISSIONS_LENGTH = 256
MAX_HTTP_ERROR_NUMERIC_HEADER_LENGTH = 10
SAFE_GITHUB_ERROR_MESSAGES = frozenset(
    {
        "API rate limit exceeded",
        "Bad credentials",
        "Forbidden",
        "Internal Server Error",
        "Not Found",
        "Problems parsing JSON",
        "Requires authentication",
        "Resource not accessible by integration",
        "Resource not accessible by personal access token",
        "Server Error",
        "Unprocessable Entity",
        "Validation Failed",
        "You have exceeded a secondary rate limit.",
    }
)
SAFE_DOCUMENTATION_PATHS = frozenset(
    {
        "/en/rest/actions/workflows",
        "/en/rest/using-the-rest-api/troubleshooting-the-rest-api",
        "/rest/actions/workflows",
        "/rest/using-the-rest-api/troubleshooting-the-rest-api",
    }
)
KNOWN_GITHUB_PERMISSION_NAMES = frozenset(
    {
        "actions",
        "administration",
        "attestations",
        "checks",
        "code",
        "code-scanning-alerts",
        "code_scanning_alerts",
        "commit-statuses",
        "commit_statuses",
        "contents",
        "custom-properties",
        "custom_properties",
        "dependabot-alerts",
        "dependabot_alerts",
        "deployments",
        "discussions",
        "environments",
        "issues",
        "metadata",
        "pages",
        "pull-requests",
        "pull_requests",
        "repository-projects",
        "repository_projects",
        "security-events",
        "security_events",
        "secret-scanning-alerts",
        "secret_scanning_alerts",
        "secrets",
        "statuses",
        "vulnerability-alerts",
        "vulnerability_alerts",
        "workflows",
    }
)
SAFE_GITHUB_PERMISSION_VALUES = frozenset({"read", "write"})
SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9]{4}:[A-Za-z0-9]{6}:[A-Za-z0-9]{6}:[A-Za-z0-9]{6}:[A-Za-z0-9]{8}")
SAFE_NUMERIC_HEADER = re.compile(r"\d{1,10}")


class PublisherError(RuntimeError):
    """A fail-closed publisher error."""

    def __init__(self, message: str, *, category: str = "blocked") -> None:
        super().__init__(message)
        self.category = category


class ApiError(PublisherError):
    def __init__(self, method: str, path: str, status: int, detail: str) -> None:
        safe_detail = detail[:MAX_API_ERROR_DETAIL_LENGTH].replace("\r", " ").replace("\n", " ")
        category = "infrastructure" if status >= 500 or status == 429 else "blocked"
        super().__init__(
            f"GitHub API {method} {path} returned HTTP {status}: {safe_detail}",
            category=category,
        )
        self.method = method
        self.path = path
        self.status = status


def _bounded_text(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        return None
    value = value.strip()
    return value or None


def _safe_error_message(value: Any, *, token: str) -> str | None:
    message = _bounded_text(value, limit=MAX_HTTP_ERROR_MESSAGE_LENGTH)
    if message is None or (token and token in message) or message not in SAFE_GITHUB_ERROR_MESSAGES:
        return None
    return message


def _safe_documentation_url(value: Any, *, token: str) -> str | None:
    url = _bounded_text(value, limit=MAX_HTTP_ERROR_DOCUMENTATION_URL_LENGTH)
    if url is None or (token and token in url) or any(character.isspace() for character in url):
        return None
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "docs.github.com"
            or parsed.query
            or parsed.fragment
            or parsed.path not in SAFE_DOCUMENTATION_PATHS
        ):
            return None
    except ValueError:
        return None
    return url


def _safe_status(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if 100 <= value <= 599 else None
    value = _bounded_text(value, limit=3)
    return value if value is not None and value.isdigit() and 100 <= int(value) <= 599 else None


def _response_header(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        try:
            value = getter(name)
        except (AttributeError, TypeError):
            value = None
        if isinstance(value, str):
            return value
    items = getattr(headers, "items", None)
    if not callable(items):
        return None
    try:
        for key, value in items():
            if isinstance(key, str) and key.lower() == name.lower() and isinstance(value, str):
                return value
    except (AttributeError, TypeError):
        return None
    return None


def _safe_response_header(
    headers: Any,
    name: str,
    *,
    token: str,
    limit: int,
    pattern: re.Pattern[str],
) -> str | None:
    value = _bounded_text(_response_header(headers, name), limit=limit)
    if value is None or (token and token in value) or not pattern.fullmatch(value):
        return None
    return value


def _safe_accepted_permissions(value: Any, *, token: str) -> str | None:
    permissions = _bounded_text(value, limit=MAX_HTTP_ERROR_PERMISSIONS_LENGTH)
    if permissions is None or (token and token in permissions):
        return None

    groups: list[str] = []
    for group in permissions.split(";"):
        pairs = [pair.strip() for pair in group.split(",")]
        if not pairs or any(not pair or pair.count("=") != 1 for pair in pairs):
            return None
        seen_names: set[str] = set()
        canonical_pairs: list[str] = []
        for pair in pairs:
            name, value = (part.strip() for part in pair.split("="))
            if (
                not name
                or not value
                or name in seen_names
                or name not in KNOWN_GITHUB_PERMISSION_NAMES
                or value not in SAFE_GITHUB_PERMISSION_VALUES
            ):
                return None
            seen_names.add(name)
            canonical_pairs.append(f"{name}={value}")
        groups.append(", ".join(canonical_pairs))
    return "; ".join(groups)


def _http_error_diagnostics(status: Any, headers: Any, body: Any, *, token: str) -> str:
    http_status = _safe_status(status)
    body_status = _safe_status(body.get("status")) if isinstance(body, dict) else None
    if body_status is not None and body_status != http_status:
        body_status = None

    parts: list[str] = []
    if body_status is not None:
        parts.append(f"status={body_status}")
    elif http_status is not None:
        parts.append(f"status={http_status}")

    if isinstance(body, dict):
        message = _safe_error_message(body.get("message"), token=token)
        documentation_url = _safe_documentation_url(body.get("documentation_url"), token=token)
        if message is not None:
            parts.append(f"message={message}")
        if documentation_url is not None:
            parts.append(f"documentation_url={documentation_url}")

    allowlisted_headers = (
        (
            "X-GitHub-Request-Id",
            MAX_HTTP_ERROR_REQUEST_ID_LENGTH,
            SAFE_REQUEST_ID,
        ),
        (
            "X-RateLimit-Remaining",
            MAX_HTTP_ERROR_NUMERIC_HEADER_LENGTH,
            SAFE_NUMERIC_HEADER,
        ),
        (
            "Retry-After",
            MAX_HTTP_ERROR_NUMERIC_HEADER_LENGTH,
            SAFE_NUMERIC_HEADER,
        ),
    )
    for name, limit, pattern in allowlisted_headers:
        value = _safe_response_header(headers, name, token=token, limit=limit, pattern=pattern)
        if value is not None:
            parts.append(f"{name}={value}")
    permissions = _safe_accepted_permissions(
        _response_header(headers, "X-Accepted-GitHub-Permissions"),
        token=token,
    )
    if permissions is not None:
        parts.append(f"X-Accepted-GitHub-Permissions={permissions}")
    return "; ".join(parts) or "HTTP error"


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: Any
    headers: Mapping[str, str] = ()


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json_body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse: ...


class UrllibTransport:
    """Small stdlib-only GitHub transport; the token is never printed."""

    def __init__(self, token: str, *, api_root: str = "https://api.github.com") -> None:
        if not token:
            raise PublisherError("GITHUB_TOKEN is required; no alternate token is accepted")
        self._token = token
        self._api_root = api_root.rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json_body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        query = urlencode({k: str(v) for k, v in (params or {}).items()})
        url = f"{self._api_root}{path}"
        if query:
            url += f"?{query}"
        request_headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "obs-voice-command-w11-010",
        }
        request_headers.update(headers or {})
        data = None
        if json_body is not None:
            request_headers["Content-Type"] = "application/json"
            data = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
        request = Request(url, data=data, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read()
                response_headers = {k.lower(): v for k, v in response.headers.items()}
                body = json.loads(raw.decode("utf-8")) if raw else None
                return HttpResponse(response.status, body, response_headers)
        except HTTPError as exc:
            raw = exc.read()
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = {}
            detail = _http_error_diagnostics(
                exc.code,
                getattr(exc, "headers", None),
                body,
                token=self._token,
            )
            raise ApiError(method, path, exc.code, detail) from exc
        except (OSError, URLError, TimeoutError) as exc:
            raise PublisherError(f"GitHub API transport failed: {type(exc).__name__}", category="infrastructure") from exc


class GithubApi:
    def __init__(self, transport: HttpTransport, repository: str) -> None:
        if not re.fullmatch(r"[^/]+/[^/]+", repository):
            raise PublisherError("repository must be owner/name")
        self.transport = transport
        self.repository = repository

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json_body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        response = self.transport.request(
            method,
            path,
            params=params,
            json_body=json_body,
            headers=headers,
        )
        if not 200 <= response.status < 300:
            detail = response.body.get("message", "request failed") if isinstance(response.body, dict) else "request failed"
            raise ApiError(method, path, response.status, str(detail))
        return response.body

    def get(self, path: str, *, params: Mapping[str, str | int] | None = None) -> Any:
        return self._call("GET", path, params=params)

    def post(self, path: str, body: Mapping[str, Any]) -> Any:
        return self._call("POST", path, json_body=body)

    def patch(self, path: str, body: Mapping[str, Any]) -> Any:
        return self._call("PATCH", path, json_body=body)

    def put(self, path: str, body: Mapping[str, Any]) -> Any:
        return self._call("PUT", path, json_body=body)

    def repository_info(self) -> Mapping[str, Any]:
        body = self.get(f"/repos/{self.repository}")
        if not isinstance(body, dict):
            raise PublisherError("repository API returned malformed data")
        return body

    def workflow(self, selector: str) -> Mapping[str, Any]:
        body = self.get(f"/repos/{self.repository}/actions/workflows/{selector}")
        if not isinstance(body, dict):
            raise PublisherError("workflow API returned malformed data")
        return body

    def run(self, run_id: int, attempt: int | None = None) -> Mapping[str, Any]:
        suffix = f"/attempts/{attempt}" if attempt is not None else ""
        body = self.get(f"/repos/{self.repository}/actions/runs/{run_id}{suffix}")
        if not isinstance(body, dict):
            raise PublisherError("run API returned malformed data")
        return body

    def jobs(self, run_id: int, attempt: int | None = None) -> list[Mapping[str, Any]]:
        values: list[Mapping[str, Any]] = []
        page = 1
        suffix = f"/attempts/{attempt}" if attempt is not None else ""
        while True:
            body = self.get(
                f"/repos/{self.repository}/actions/runs/{run_id}{suffix}/jobs",
                params={"per_page": 100, "page": page},
            )
            if not isinstance(body, dict) or not isinstance(body.get("jobs"), list):
                raise PublisherError("jobs API returned malformed data")
            values.extend(_mappings(body["jobs"], "jobs"))
            if len(body["jobs"]) < 100:
                return values
            page += 1

    def check_suites(self, sha: str) -> list[Mapping[str, Any]]:
        values: list[Mapping[str, Any]] = []
        page = 1
        while True:
            body = self.get(
                f"/repos/{self.repository}/commits/{sha}/check-suites",
                params={"per_page": 100, "page": page},
            )
            if not isinstance(body, dict) or not isinstance(body.get("check_suites"), list):
                raise PublisherError("check-suite API returned malformed data")
            values.extend(_mappings(body["check_suites"], "check_suites"))
            if len(body["check_suites"]) < 100:
                return values
            page += 1

    def commit_pulls(self, sha: str) -> list[Mapping[str, Any]]:
        return _mappings(
            self._pages(
                f"/repos/{self.repository}/commits/{sha}/pulls",
                headers={"Accept": "application/vnd.github+json"},
            ),
            "commit_pulls",
        )

    def pull(self, number: int) -> Mapping[str, Any]:
        body = self.get(f"/repos/{self.repository}/pulls/{number}")
        if not isinstance(body, dict):
            raise PublisherError("pull request API returned malformed data")
        return body

    def branch_tip(self, branch: str) -> str:
        body = self.get(f"/repos/{self.repository}/git/ref/heads/{branch}")
        try:
            sha = body["object"]["sha"]
        except (KeyError, TypeError):
            raise PublisherError("protected branch ref API returned malformed data") from None
        _require_sha(sha, "protected branch tip")
        return sha

    def compare(self, base: str, head: str) -> Mapping[str, Any]:
        body = self.get(f"/repos/{self.repository}/compare/{base}...{head}")
        if not isinstance(body, dict):
            raise PublisherError("compare API returned malformed data")
        return body

    def manifest(self, path: str, ref: str) -> str:
        body = self.get(f"/repos/{self.repository}/contents/{path}", params={"ref": ref})
        if not isinstance(body, dict) or body.get("type") != "file" or body.get("encoding") != "base64":
            raise PublisherError("manifest contents API returned an unexpected object")
        try:
            content = body["content"]
            if not isinstance(content, str):
                raise ValueError("content is not text")
            # GitHub Contents 可能以折行 Base64 回傳；折行是傳輸格式，不是資料。
            normalized = content.replace("\r", "").replace("\n", "")
            raw = base64.b64decode(normalized, validate=True)
            return raw.decode("utf-8")
        except (KeyError, ValueError, UnicodeDecodeError, TypeError) as exc:
            raise PublisherError("manifest contents API returned invalid UTF-8 base64") from exc

    def workflow_runs(self, workflow_id: int) -> list[Mapping[str, Any]]:
        values: list[Mapping[str, Any]] = []
        page = 1
        while True:
            body = self.get(
                f"/repos/{self.repository}/actions/workflows/{workflow_id}/runs",
                params={"branch": "develop", "event": "push", "per_page": 100, "page": page},
            )
            if not isinstance(body, dict) or not isinstance(body.get("workflow_runs"), list):
                raise PublisherError("workflow-runs API returned malformed data")
            values.extend(_mappings(body["workflow_runs"], "workflow_runs"))
            if len(body["workflow_runs"]) < 100:
                return values
            page += 1

    def issue_comments(self, number: int) -> list[Mapping[str, Any]]:
        return _mappings(
            self._pages(f"/repos/{self.repository}/issues/{number}/comments"),
            "issue_comments",
        )

    def issues(self) -> list[Mapping[str, Any]]:
        return _mappings(
            self._pages(
                f"/repos/{self.repository}/issues",
                params={"state": "all", "per_page": 100},
            ),
            "issues",
        )

    def create_issue(self, title: str, body: str, labels: Sequence[str]) -> Mapping[str, Any]:
        value = self.post(
            f"/repos/{self.repository}/issues",
            {"title": title, "body": body, "labels": list(labels)},
        )
        if not isinstance(value, dict):
            raise PublisherError("issue create API returned malformed data")
        return value

    def update_issue(self, number: int, body: str, *, state: str = "open") -> Mapping[str, Any]:
        value = self.patch(
            f"/repos/{self.repository}/issues/{number}",
            {"body": body, "state": state},
        )
        if not isinstance(value, dict):
            raise PublisherError("issue update API returned malformed data")
        return value

    def update_comment(self, comment_id: int, body: str) -> Mapping[str, Any]:
        value = self.patch(f"/repos/{self.repository}/issues/comments/{comment_id}", {"body": body})
        if not isinstance(value, dict):
            raise PublisherError("comment update API returned malformed data")
        return value

    def create_comment(self, number: int, body: str) -> Mapping[str, Any]:
        value = self.post(f"/repos/{self.repository}/issues/{number}/comments", {"body": body})
        if not isinstance(value, dict):
            raise PublisherError("comment create API returned malformed data")
        return value

    def set_labels(self, number: int, labels: Sequence[str]) -> list[Mapping[str, Any]]:
        value = self.put(f"/repos/{self.repository}/issues/{number}/labels", {"labels": list(labels)})
        if not isinstance(value, list):
            raise PublisherError("issue label API returned malformed data")
        return _mappings(value, "issue_labels")

    def _pages(
        self,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> list[Any]:
        values: list[Any] = []
        page = 1
        base_params = dict(params or {})
        while page <= 100:
            page_params = {**base_params, "per_page": 100, "page": page}
            body = self._call("GET", path, params=page_params, headers=headers)
            if not isinstance(body, list):
                raise PublisherError(f"paginated API {path} returned malformed data")
            values.extend(body)
            if len(body) < 100:
                return values
            page += 1
        raise PublisherError(f"pagination limit exceeded for {path}")


def _mappings(values: Sequence[Any], label: str) -> list[Mapping[str, Any]]:
    if not all(isinstance(value, dict) for value in values):
        raise PublisherError(f"{label} API returned a non-object item")
    return list(values)  # type: ignore[return-value]


def _require_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not VALID_SHA.fullmatch(value):
        raise PublisherError(f"{label} is not an exact 40-character commit SHA")
    return value.lower()


def _require_url(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("https://"):
        raise PublisherError(f"{label} is not a trusted HTTPS URL")
    return value


def _parse_machine_payload(body: Any, marker: str) -> Mapping[str, Any] | None:
    if not isinstance(body, str) or body.count(marker) == 0:
        return None
    if body.count(marker) != 1:
        raise PublisherError("projection contains duplicate machine markers")
    match = re.search(r"```json\n(.*?)\n```", body, flags=re.DOTALL)
    if not match:
        raise PublisherError("projection marker has no JSON payload")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise PublisherError("projection contains malformed JSON") from exc
    if not isinstance(payload, dict):
        raise PublisherError("projection JSON must be an object")
    return payload


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PublisherError(f"{label} must be a positive integer")
    return value


def _nullable_positive(value: str, label: str) -> int | None:
    value = value.strip()
    if value.lower() in NULL:
        return None
    if not value.isdigit() or int(value) <= 0:
        raise PublisherError(f"{label} must be null or a positive integer")
    return int(value)


@dataclass(frozen=True)
class RegistrationAttempt:
    pr_number: int
    predecessor_pr_number: int | None
    repair_issue_number: int | None


@dataclass(frozen=True)
class Registration:
    version: int
    attempts: tuple[RegistrationAttempt, ...]
    ticket_id: str = ""
    completion_profile: str | None = None

    @property
    def root_pr_number(self) -> int:
        return self.attempts[0].pr_number


def parse_registration(manifest: str, ticket_id: str) -> Registration:
    """Parse only the deliberately narrow registration block, fail closed."""

    lines = manifest.splitlines()
    ticket_matches = [
        index
        for index, line in enumerate(lines)
        if re.fullmatch(rf"  - id:\s*{re.escape(ticket_id)}\s*", line)
    ]
    if len(ticket_matches) != 1:
        raise PublisherError(f"manifest must contain exactly one {ticket_id} ticket")
    start = ticket_matches[0]
    end = next(
        (index for index in range(start + 1, len(lines)) if re.fullmatch(r"  - id:\s*\S+\s*", lines[index])),
        len(lines),
    )
    block = lines[start:end]
    completion_match = next(
        (
            re.fullmatch(r"    completion_profile:\s*(\S+)\s*", line)
            for line in block
            if line.startswith("    completion_profile:")
        ),
        None,
    )
    completion_profile = completion_match.group(1) if completion_match else None
    registration_indexes = [
        index for index, line in enumerate(block) if line == "    post_merge_registration:"
    ]
    if len(registration_indexes) != 1:
        raise PublisherError("manifest must contain exactly one post_merge_registration block")
    index = registration_indexes[0] + 1
    child: list[str] = []
    while index < len(block):
        line = block[index]
        if line and not line.startswith("      "):
            break
        if line.strip():
            child.append(line)
        index += 1
    if len(child) < 2 or child[0] != "      version: 1" or child[1] != "      attempts:":
        raise PublisherError("post_merge_registration must start with version: 1 and attempts:")
    entries: list[RegistrationAttempt] = []
    index = 2
    while index < len(child):
        if index + 2 >= len(child):
            raise PublisherError("incomplete post_merge_registration attempt")
        first = re.fullmatch(r"        - pr_number:\s*(\S+)\s*", child[index])
        predecessor = re.fullmatch(r"          predecessor_pr_number:\s*(\S+)\s*", child[index + 1])
        repair = re.fullmatch(r"          repair_issue_number:\s*(\S+)\s*", child[index + 2])
        if not first or not predecessor or not repair:
            raise PublisherError("malformed post_merge_registration attempt")
        if not first.group(1).isdigit() or int(first.group(1)) <= 0:
            raise PublisherError("post_merge_registration pr_number must be positive")
        entries.append(
            RegistrationAttempt(
                pr_number=int(first.group(1)),
                predecessor_pr_number=_nullable_positive(
                    predecessor.group(1), "predecessor_pr_number"
                ),
                repair_issue_number=_nullable_positive(repair.group(1), "repair_issue_number"),
            )
        )
        index += 3
    registration = Registration(
        REGISTRATION_VERSION,
        tuple(entries),
        ticket_id=ticket_id,
        completion_profile=completion_profile,
    )
    validate_registration(registration)
    return registration


def registered_registrations(manifest: str) -> dict[str, Registration]:
    """Return every active ticket with a post-merge registration block."""

    lines = manifest.splitlines()
    ticket_ids: list[str] = []
    for index, line in enumerate(lines):
        match = re.fullmatch(r"  - id:\s*(\S+)\s*", line)
        if not match:
            continue
        end = next(
            (
                candidate
                for candidate in range(index + 1, len(lines))
                if re.fullmatch(r"  - id:\s*\S+\s*", lines[candidate])
            ),
            len(lines),
        )
        if "    post_merge_registration:" in lines[index:end]:
            ticket_ids.append(match.group(1))
    if len(set(ticket_ids)) != len(ticket_ids):
        raise PublisherError("manifest contains duplicate registered ticket ids")
    return {ticket_id: parse_registration(manifest, ticket_id) for ticket_id in ticket_ids}


def discover_registered_ticket(
    manifest: str,
    source_pr_number: int,
) -> Registration:
    """Find the unique registered ticket whose lineage contains a source PR."""

    matches = [
        registration
        for registration in registered_registrations(manifest).values()
        if source_pr_number in {attempt.pr_number for attempt in registration.attempts}
    ]
    if len(matches) != 1:
        raise PublisherError(
            "source PR must resolve to exactly one registered originating ticket"
        )
    return matches[0]


def validate_registration(registration: Registration) -> None:
    if registration.version != REGISTRATION_VERSION or not registration.attempts:
        raise PublisherError("registration version or attempts is invalid")
    numbers = [attempt.pr_number for attempt in registration.attempts]
    if len(set(numbers)) != len(numbers):
        raise PublisherError("registration contains duplicate PR numbers")
    root_count = 0
    for index, attempt in enumerate(registration.attempts):
        if index == 0:
            if attempt.predecessor_pr_number is not None or attempt.repair_issue_number is not None:
                raise PublisherError("originating registration must have null predecessor and repair issue")
            root_count += 1
            continue
        if attempt.predecessor_pr_number != registration.attempts[index - 1].pr_number:
            raise PublisherError("repair registration must name its immediate predecessor PR")
        if attempt.repair_issue_number is None:
            raise PublisherError("repair registration must name an existing repair issue")
    if root_count != 1:
        raise PublisherError("registration must have exactly one originating attempt")


def lineage_key(repository: str, ticket_id: str, root_pr_number: int) -> str:
    raw = f"{repository.lower()}|{ticket_id}|{root_pr_number}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _repair_projection(failures: int) -> tuple[str, str, tuple[str, ...]]:
    """回傳 repair issue 的投影狀態、controller action 與 canonical labels。"""

    if failures >= 4:
        return "blocked", "needs_human", ("type:repair", "status:blocked", "needs-human")
    if failures == 3:
        return "blocked", "arbitration_requested", ("type:repair", "status:blocked")
    return "ready", "controller_may_dispatch_implementer", ("type:repair", "status:ready")


@dataclass(frozen=True)
class PublisherConfig:
    repository: str
    ticket_id: str | None = None
    workflow_path: str = DEFAULT_WORKFLOW_PATH
    manifest_path: str = DEFAULT_MANIFEST_PATH
    required_jobs: tuple[str, ...] = DEFAULT_REQUIRED_JOBS
    publisher_logins: tuple[str, ...] = ("github-actions[bot]",)
    finalizer_run_id: int | None = None
    finalizer_run_url: str | None = None


@dataclass(frozen=True)
class VerifiedRun:
    run_id: int
    run_attempt: int
    sha: str
    workflow_id: int
    workflow_path: str
    url: str
    conclusion: str
    quality: str
    check_suite_id: int
    provider: str
    job_conclusions: Mapping[str, str]
    failed_steps: tuple[str, ...]


@dataclass(frozen=True)
class VerifiedPull:
    number: int
    merge_sha: str
    url: str


@dataclass(frozen=True)
class PublisherResult:
    status: str
    action: str
    source_run_id: int
    source_run_attempt: int
    source_sha: str
    source_pr_number: int | None
    latest_pr_number: int | None
    lineage_id: str | None
    repair_issue_number: int | None
    created_repair_issue: bool
    consecutive_code_failures: int
    infrastructure_failures: int
    evidence: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "action": self.action,
            "source_run_id": self.source_run_id,
            "source_run_attempt": self.source_run_attempt,
            "source_sha": self.source_sha,
            "source_pr_number": self.source_pr_number,
            "latest_pr_number": self.latest_pr_number,
            "lineage_id": self.lineage_id,
            "repair_issue_number": self.repair_issue_number,
            "created_repair_issue": self.created_repair_issue,
            "consecutive_code_failures": self.consecutive_code_failures,
            "infrastructure_failures": self.infrastructure_failures,
            "evidence": dict(self.evidence),
        }


class PostMergePublisher:
    # The workflow also serializes finalizer jobs across processes.  This
    # process-local lock lets fake-transport tests exercise the same
    # find-or-create transaction when two publisher objects share one process.
    _mutation_lock = threading.RLock()

    def __init__(self, api: GithubApi, config: PublisherConfig) -> None:
        self.api = api
        self.config = config
        self._workflow_id: int | None = None
        self._resolved_ticket_id: str | None = config.ticket_id

    def _ticket_id(self) -> str:
        if not self._resolved_ticket_id:
            raise PublisherError("publisher has not resolved a registered ticket")
        return self._resolved_ticket_id

    def _trusted_workflow_id(self) -> int:
        if self._workflow_id is not None:
            return self._workflow_id
        repository = self.api.repository_info()
        if str(repository.get("full_name", "")).lower() != self.config.repository.lower():
            raise PublisherError("GitHub repository identity does not match the trusted repository")
        selector = self.config.workflow_path.rsplit("/", 1)[-1]
        workflow = self.api.workflow(selector)
        path = workflow.get("path")
        if path != self.config.workflow_path:
            raise PublisherError("workflow API path does not match post-merge producer")
        self._workflow_id = _positive_int(workflow.get("id"), "workflow id")
        return self._workflow_id

    def _verify_run(self, run_id: int, expected_attempt: int | None = None) -> VerifiedRun:
        workflow_id = self._trusted_workflow_id()
        run = self.api.run(run_id, expected_attempt)
        if _positive_int(run.get("id"), "run id") != run_id:
            raise PublisherError("source run identity changed during verification")
        repository = run.get("repository")
        if not isinstance(repository, dict) or str(repository.get("full_name", "")).lower() != self.config.repository.lower():
            raise PublisherError("source run repository is not the trusted repository")
        if _positive_int(run.get("workflow_id"), "source workflow id") != workflow_id:
            raise PublisherError("source run belongs to a different workflow")
        if run.get("event") != "push" or run.get("head_branch") != "develop":
            raise PublisherError("source run is not a protected develop push")
        if run.get("status") != "completed":
            raise PublisherError("source run is not completed")
        run_conclusion = run.get("conclusion")
        if not isinstance(run_conclusion, str) or not run_conclusion:
            raise PublisherError("source run has no completion conclusion")
        attempt = _positive_int(run.get("run_attempt"), "source run attempt")
        if expected_attempt is not None and attempt != expected_attempt:
            raise PublisherError("source run attempt does not match the trusted event")
        sha = _require_sha(run.get("head_sha"), "source run head_sha")
        jobs = self.api.jobs(run_id, attempt)
        by_name: dict[str, Mapping[str, Any]] = {}
        for job in jobs:
            name = job.get("name")
            if not isinstance(name, str) or not name:
                raise PublisherError("source job has no stable name")
            if name in by_name:
                raise PublisherError(f"source run has duplicate required job name: {name}")
            by_name[name] = job
        missing = [name for name in self.config.required_jobs if name not in by_name]
        if missing:
            raise PublisherError(f"source run is missing required jobs: {', '.join(missing)}")
        job_conclusions: dict[str, str] = {}
        failed_steps: list[str] = []
        direct_failure_kinds: list[str] = []
        for name in self.config.required_jobs:
            conclusion = by_name[name].get("conclusion")
            if not isinstance(conclusion, str):
                raise PublisherError(f"required job {name} has no conclusion")
            job_conclusions[name] = conclusion
            if conclusion != "success":
                steps = by_name[name].get("steps", [])
                if not isinstance(steps, list):
                    raise PublisherError(f"required job {name} has malformed steps")
                job_failed_steps: list[tuple[str, str]] = []
                for step in steps:
                    if not isinstance(step, dict):
                        raise PublisherError(f"required job {name} has a malformed step")
                    step_conclusion = step.get("conclusion")
                    if step_conclusion in (None, "success", "skipped"):
                        continue
                    step_name = step.get("name")
                    if not isinstance(step_name, str) or not step_name:
                        direct_failure_kinds.append("unproven")
                        continue
                    failed_steps.append(f"{name}: {step_name}")
                    job_failed_steps.append((step_name, str(step_conclusion)))
                if name in AGGREGATE_JOB_NAMES:
                    # required / gate only reflects its dependencies.  Its own
                    # failing shell step cannot prove a code failure.
                    continue
                if conclusion != "failure" or not job_failed_steps:
                    direct_failure_kinds.append("infrastructure")
                elif all(
                    step_conclusion == "failure"
                    and step_name in CODE_ATTRIBUTABLE_STEP_NAMES
                    for step_name, step_conclusion in job_failed_steps
                ):
                    direct_failure_kinds.append("code")
                else:
                    direct_failure_kinds.append("infrastructure")
        source_check_suite_id = _positive_int(
            run.get("check_suite_id"), "source run check_suite_id"
        )
        suites = [
            suite
            for suite in self.api.check_suites(sha)
            if suite.get("id") == source_check_suite_id
        ]
        trusted_suites = [suite for suite in suites if _is_actions_suite(suite) and suite.get("head_sha") == sha]
        if len(trusted_suites) != 1:
            raise PublisherError("source run must have exactly one matching GitHub Actions check suite")
        suite = trusted_suites[0]
        suite_id = _positive_int(suite.get("id"), "check suite id")
        if suite_id != source_check_suite_id:
            raise PublisherError("source run check suite identity changed during verification")
        provider = str((suite.get("app") or {}).get("slug") or (suite.get("app") or {}).get("name") or "")
        if not provider:
            raise PublisherError("source check suite has no provider identity")
        suite_conclusion = suite.get("conclusion")
        if run_conclusion == "success" and suite_conclusion == "success" and all(
            conclusion == "success" for conclusion in job_conclusions.values()
        ):
            quality = "pass"
        elif (
            run_conclusion == "failure"
            and suite_conclusion == "failure"
            and direct_failure_kinds
            and all(kind == "code" for kind in direct_failure_kinds)
            and any(
                conclusion == "failure"
                for name, conclusion in job_conclusions.items()
                if name not in AGGREGATE_JOB_NAMES
            )
        ):
            quality = "code_failure"
        else:
            quality = "infrastructure_failure"
        return VerifiedRun(
            run_id=run_id,
            run_attempt=attempt,
            sha=sha,
            workflow_id=workflow_id,
            workflow_path=self.config.workflow_path,
            url=_require_url(run.get("html_url"), "source run URL"),
            conclusion=str(run_conclusion or ""),
            quality=quality,
            check_suite_id=suite_id,
            provider=provider,
            job_conclusions=job_conclusions,
            failed_steps=tuple(failed_steps),
        )

    def _verified_pull_for_sha(self, sha: str) -> VerifiedPull:
        candidates: list[VerifiedPull] = []
        for listed in self.api.commit_pulls(sha):
            number = listed.get("number")
            if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
                continue
            pull = self.api.pull(number)
            base = pull.get("base")
            base_repo = base.get("repo") if isinstance(base, dict) else None
            if not isinstance(base, dict) or base.get("ref") != "develop":
                continue
            if not isinstance(base_repo, dict) or str(base_repo.get("full_name", "")).lower() != self.config.repository.lower():
                continue
            if not pull.get("merged_at") or pull.get("merge_commit_sha") != sha:
                continue
            candidates.append(
                VerifiedPull(
                    number=number,
                    merge_sha=sha,
                    url=_require_url(pull.get("html_url"), f"PR #{number} URL"),
                )
            )
        if len(candidates) != 1:
            raise PublisherError("source SHA must resolve to exactly one merged develop PR")
        return candidates[0]

    def _registered_context(
        self,
        source_run: VerifiedRun,
        source_pull: VerifiedPull,
    ) -> tuple[Registration, Registration, dict[int, VerifiedPull], str, str]:
        source_manifest_text = self.api.manifest(self.config.manifest_path, source_run.sha)
        if self.config.ticket_id is None:
            source_manifest = discover_registered_ticket(source_manifest_text, source_pull.number)
            self._resolved_ticket_id = source_manifest.ticket_id
        else:
            self._resolved_ticket_id = self.config.ticket_id
            source_manifest = parse_registration(source_manifest_text, self._ticket_id())
        protected_tip = self.api.branch_tip("develop")
        compare_tip = self.api.compare(source_run.sha, protected_tip)
        if compare_tip.get("status") not in {"ahead", "identical"}:
            raise PublisherError("protected develop tip is not descended from the source SHA")
        current_manifest = parse_registration(
            self.api.manifest(self.config.manifest_path, protected_tip),
            self._ticket_id(),
        )
        if current_manifest.attempts[: len(source_manifest.attempts)] != source_manifest.attempts:
            raise PublisherError("current protected registration is not an extension of the source registration")
        if source_pull.number not in {attempt.pr_number for attempt in source_manifest.attempts}:
            raise PublisherError("source merged PR is not registered for this ticket")
        if source_manifest.root_pr_number != current_manifest.root_pr_number:
            raise PublisherError("source and protected registrations have different lineage roots")
        pulls: dict[int, VerifiedPull] = {}
        for index, attempt in enumerate(current_manifest.attempts):
            pull = self.api.pull(attempt.pr_number)
            base = pull.get("base")
            base_repo = base.get("repo") if isinstance(base, dict) else None
            if (
                not pull.get("merged_at")
                or not isinstance(base, dict)
                or base.get("ref") != "develop"
                or not isinstance(base_repo, dict)
                or str(base_repo.get("full_name", "")).lower() != self.config.repository.lower()
            ):
                raise PublisherError(f"registered PR #{attempt.pr_number} is not a merged develop PR")
            merge_sha = _require_sha(pull.get("merge_commit_sha"), f"PR #{attempt.pr_number} merge_commit_sha")
            if index and current_manifest.attempts[index - 1].pr_number in pulls:
                previous = pulls[current_manifest.attempts[index - 1].pr_number].merge_sha
                if self.api.compare(previous, merge_sha).get("status") not in {"ahead", "identical"}:
                    raise PublisherError("registered PR ancestry is not monotonic")
            pulls[attempt.pr_number] = VerifiedPull(
                number=attempt.pr_number,
                merge_sha=merge_sha,
                url=_require_url(pull.get("html_url"), f"PR #{attempt.pr_number} URL"),
            )
        if pulls.get(source_pull.number) != source_pull:
            raise PublisherError("source PR registration does not agree with source run SHA")
        lineage = lineage_key(self.config.repository, self._ticket_id(), current_manifest.root_pr_number)
        return source_manifest, current_manifest, pulls, lineage, protected_tip

    def _revalidate_before_mutation(
        self,
        source_run: VerifiedRun,
        source_pull: VerifiedPull,
        source_registration: Registration,
        current_registration: Registration,
        pulls: Mapping[int, VerifiedPull],
        lineage: str,
        protected_tip: str,
        require_latest_source: bool = False,
    ) -> tuple[
        VerifiedRun,
        VerifiedPull,
        Registration,
        Registration,
        dict[int, VerifiedPull],
        str,
        str,
    ]:
        """在任何 GitHub mutation 前重讀受保護 registration、PR 與 run。"""

        fresh_run = self._verify_run(source_run.run_id, source_run.run_attempt)
        if fresh_run != source_run:
            raise PublisherError("source run changed before publication")
        fresh_pull = self._verified_pull_for_sha(fresh_run.sha)
        (
            fresh_source_registration,
            fresh_current_registration,
            fresh_pulls,
            fresh_lineage,
            fresh_tip,
        ) = self._registered_context(fresh_run, fresh_pull)
        if (
            fresh_pull != source_pull
            or fresh_source_registration != source_registration
            or fresh_current_registration != current_registration
            or fresh_pulls != dict(pulls)
            or fresh_lineage != lineage
            or fresh_tip != protected_tip
        ):
            raise PublisherError(
                "protected manifest or merged PR changed before publication"
            )
        latest = self._latest_verified_run_for_sha(fresh_run)
        if (latest.run_id, latest.run_attempt) != (fresh_run.run_id, fresh_run.run_attempt):
            raise PublisherError("source run became stale before publication")
        latest_attempt = fresh_current_registration.attempts[-1]
        latest_pull = fresh_pulls[latest_attempt.pr_number]
        if require_latest_source and latest_pull != fresh_pull:
            raise PublisherError("source PR is no longer the latest protected registration")
        return (
            fresh_run,
            fresh_pull,
            fresh_source_registration,
            fresh_current_registration,
            fresh_pulls,
            fresh_lineage,
            fresh_tip,
        )

    def _machine_payload(self, body: Any, marker: str) -> Mapping[str, Any] | None:
        return _parse_machine_payload(body, marker)

    def _trusted_projection_author(self, value: Mapping[str, Any]) -> bool:
        user = value.get("user")
        return isinstance(user, dict) and user.get("type") == "Bot" and user.get("login") in self.config.publisher_logins

    def _validate_pass_payload(
        self,
        payload: Mapping[str, Any],
        *,
        source_pull: VerifiedPull,
        run: VerifiedRun,
        lineage: str,
    ) -> None:
        if (
            payload.get("kind") != "w11-010-pass"
            or payload.get("version") != 1
            or payload.get("repository") != self.config.repository
            or payload.get("ticket_id") != self._ticket_id()
            or payload.get("lineage_id") != lineage
        ):
            raise PublisherError("PASS projection has an invalid protected schema")
        if payload.get("source_pr_number") != source_pull.number:
            raise PublisherError("PASS projection names a different merged PR")
        if _require_sha(payload.get("source_sha"), "PASS projection source_sha") != run.sha:
            raise PublisherError("PASS projection names a different source SHA")
        _positive_int(payload.get("run_id"), "PASS projection run id")
        _positive_int(payload.get("run_attempt"), "PASS projection run attempt")
        if payload.get("workflow_id") != run.workflow_id:
            raise PublisherError("PASS projection names a different workflow")
        if payload.get("workflow_path") != run.workflow_path:
            raise PublisherError("PASS projection names a different workflow path")
        if payload.get("check_suite_id") != run.check_suite_id:
            raise PublisherError("PASS projection names a different check suite")
        if payload.get("provider") != run.provider:
            raise PublisherError("PASS projection names a different check provider")

    def _validate_repair_payload(
        self, payload: Mapping[str, Any], *, lineage: str
    ) -> None:
        if (
            payload.get("kind") != "w11-010-repair"
            or payload.get("version") != 1
            or payload.get("repository") != self.config.repository
            or payload.get("ticket_id") != self._ticket_id()
            or payload.get("lineage_id") != lineage
        ):
            raise PublisherError("repair projection has an invalid protected schema")
        if not isinstance(payload.get("source_pr_number"), int) or payload["source_pr_number"] <= 0:
            raise PublisherError("repair projection source PR is invalid")
        _require_sha(payload.get("source_sha"), "repair projection source_sha")
        _positive_int(payload.get("source_run_id"), "repair projection source run id")
        _positive_int(payload.get("source_run_attempt"), "repair projection source run attempt")
        signature = payload.get("failure_signature")
        if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature):
            raise PublisherError("repair projection failure signature is invalid")
        failures = payload.get("consecutive_code_failures")
        if isinstance(failures, bool) or not isinstance(failures, int) or not 1 <= failures <= 4:
            raise PublisherError("repair projection retry count is invalid")
        projection_status = payload.get("projection_status")
        retry_action = payload.get("retry_action")
        if projection_status is not None or retry_action is not None:
            expected_status, expected_action, _ = _repair_projection(failures)
            if projection_status != expected_status or retry_action != expected_action:
                raise PublisherError("repair projection retry state is inconsistent")
        if not isinstance(payload.get("failing_jobs"), dict):
            raise PublisherError("repair projection failing jobs are invalid")
        if not isinstance(payload.get("failed_steps"), list):
            raise PublisherError("repair projection failed steps are invalid")
        if payload.get("latest_merged_pr") != payload.get("source_pr_number"):
            raise PublisherError("repair projection latest merged PR is inconsistent")
        rollback_hint = payload.get("rollback_hint")
        if not isinstance(rollback_hint, str) or "protected develop" not in rollback_hint:
            raise PublisherError("repair projection rollback hint is invalid")

    def _projection_body(self, marker: str, payload: Mapping[str, Any], lines: Sequence[str]) -> str:
        return "\n".join(
            [marker, "```json", json.dumps(dict(payload), sort_keys=True, separators=(",", ":")), "```", "", *lines]
        )

    def _publish_pr_pass(self, source_pull: VerifiedPull, run: VerifiedRun, lineage: str) -> None:
        with self._mutation_lock:
            self._publish_pr_pass_locked(source_pull, run, lineage)

    def _publish_pr_pass_locked(
        self, source_pull: VerifiedPull, run: VerifiedRun, lineage: str
    ) -> None:
        payload = {
            "kind": "w11-010-pass",
            "version": 1,
            "repository": self.config.repository,
            "ticket_id": self._ticket_id(),
            "lineage_id": lineage,
            "source_pr_number": source_pull.number,
            "source_sha": run.sha,
            "run_id": run.run_id,
            "run_attempt": run.run_attempt,
            "workflow_id": run.workflow_id,
            "workflow_path": run.workflow_path,
            "check_suite_id": run.check_suite_id,
            "provider": run.provider,
            "required_jobs": dict(run.job_conclusions),
            "finalizer_run_id": self.config.finalizer_run_id,
            "finalizer_run_url": self.config.finalizer_run_url,
        }
        body = self._projection_body(
            PASS_MARKER,
            payload,
            [
                f"W11-010 post-merge Layer A PASS for `{run.sha}`.",
                f"Verified producer run `{run.run_id}` attempt `{run.run_attempt}`: {run.url}.",
                f"Verified workflow `{run.workflow_id}` `{run.workflow_path}` and GitHub Actions check suite `{run.check_suite_id}` ({run.provider}).",
                f"Merged PR: #{source_pull.number} ({source_pull.url}).",
            ],
        )
        comments = self.api.issue_comments(source_pull.number)
        matches = []
        for comment in comments:
            payload_value = self._machine_payload(comment.get("body"), PASS_MARKER)
            if payload_value is None:
                continue
            if not self._trusted_projection_author(comment):
                raise PublisherError("PASS projection marker belongs to an untrusted author")
            self._validate_pass_payload(
                payload_value,
                source_pull=source_pull,
                run=run,
                lineage=lineage,
            )
            matches.append(comment)
        if len(matches) > 1:
            raise PublisherError("multiple trusted PASS projections exist for the verified PR")
        if matches:
            comment_id = _positive_int(matches[0].get("id"), "PASS comment id")
            self.api.update_comment(comment_id, body)
        else:
            created = self.api.create_comment(source_pull.number, body)
            if not self._trusted_projection_author(created):
                raise PublisherError("created PASS projection was not authored by the trusted bot")
            created_payload = self._machine_payload(created.get("body"), PASS_MARKER)
            if created_payload is None:
                raise PublisherError("created PASS projection lost its machine payload")
            self._validate_pass_payload(
                created_payload,
                source_pull=source_pull,
                run=run,
                lineage=lineage,
            )

    def _find_repair_issue(self, lineage: str) -> tuple[int, Mapping[str, Any]] | None:
        matches: list[tuple[int, Mapping[str, Any]]] = []
        for issue in self.api.issues():
            payload = self._machine_payload(issue.get("body"), REPAIR_MARKER)
            if payload is None:
                continue
            if payload.get("repository") != self.config.repository:
                continue
            if "ticket_id" not in payload:
                raise PublisherError("repair projection has an invalid protected schema")
            if payload.get("ticket_id") != self._ticket_id():
                continue
            if not self._trusted_projection_author(issue):
                raise PublisherError("repair marker belongs to an untrusted author")
            self._validate_repair_payload(payload, lineage=lineage)
            number = _positive_int(issue.get("number"), "repair issue number")
            matches.append((number, issue))
        if len(matches) > 1:
            raise PublisherError("multiple repair issues exist for the same protected lineage")
        return matches[0] if matches else None

    def _repair_body(
        self,
        *,
        lineage: str,
        source_pull: VerifiedPull,
        run: VerifiedRun,
        signature: str,
        failures: int,
    ) -> str:
        projection_status, retry_action, _ = _repair_projection(failures)
        payload = {
            "kind": "w11-010-repair",
            "version": 1,
            "repository": self.config.repository,
            "ticket_id": self._ticket_id(),
            "lineage_id": lineage,
            "source_pr_number": source_pull.number,
            "source_sha": run.sha,
            "source_run_id": run.run_id,
            "source_run_attempt": run.run_attempt,
            "failure_signature": signature,
            "failing_jobs": {
                name: conclusion
                for name, conclusion in run.job_conclusions.items()
                if conclusion != "success"
            },
            "failed_steps": list(run.failed_steps),
            "latest_merged_pr": source_pull.number,
            "rollback_hint": f"Revert merged PR #{source_pull.number} through protected develop; never direct-push.",
            "consecutive_code_failures": failures,
            "projection_status": projection_status,
            "retry_action": retry_action,
        }
        return self._projection_body(
            REPAIR_MARKER,
            payload,
            [
                f"W11-010 Repair: verified Layer A failure on `{run.sha}`.",
                f"Producer run `{run.run_id}` attempt `{run.run_attempt}`: {run.url}.",
                f"Merged PR: #{source_pull.number} ({source_pull.url}).",
                f"Projection state: `{projection_status}`; controller action: `{retry_action}`.",
                "The controller owns retry dispatch and ticket state; this issue is a validated projection.",
            ],
        )

    def _publish_failure(
        self,
        source_pull: VerifiedPull,
        run: VerifiedRun,
        lineage: str,
        failures: int,
    ) -> tuple[int, bool]:
        with self._mutation_lock:
            return self._publish_failure_locked(source_pull, run, lineage, failures)

    def _publish_failure_locked(
        self,
        source_pull: VerifiedPull,
        run: VerifiedRun,
        lineage: str,
        failures: int,
    ) -> tuple[int, bool]:
        signature = hashlib.sha256(
            json.dumps(
                {
                    "lineage": lineage,
                    "sha": run.sha,
                    "jobs": run.job_conclusions,
                    "steps": run.failed_steps,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        body = self._repair_body(
            lineage=lineage,
            source_pull=source_pull,
            run=run,
            signature=signature,
            failures=failures,
        )
        found = self._find_repair_issue(lineage)
        _, _, labels = _repair_projection(failures)
        if found:
            number, issue = found
            current_payload = self._machine_payload(issue.get("body"), REPAIR_MARKER)
            if current_payload is None:
                raise PublisherError("repair issue candidate lost its machine payload")
            self.api.update_issue(number, body, state="open")
            self.api.set_labels(number, labels)
            return number, False
        created = self.api.create_issue(
            f"W11-010 repair for {self._ticket_id()} lineage {lineage}",
            body,
            labels,
        )
        number = _positive_int(created.get("number"), "created repair issue number")
        if not self._trusted_projection_author(created):
            raise PublisherError("created repair issue was not authored by the trusted bot")
        created_payload = self._machine_payload(created.get("body"), REPAIR_MARKER)
        if created_payload is None:
            raise PublisherError("created repair issue lost its machine payload")
        self._validate_repair_payload(created_payload, lineage=lineage)
        self.api.set_labels(number, labels)
        return number, True

    def _close_repair_if_current(self, lineage: str, run: VerifiedRun) -> int | None:
        with self._mutation_lock:
            return self._close_repair_if_current_locked(lineage, run)

    def _close_repair_if_current_locked(self, lineage: str, run: VerifiedRun) -> int | None:
        found = self._find_repair_issue(lineage)
        if not found:
            return None
        number, issue = found
        payload = self._machine_payload(issue.get("body"), REPAIR_MARKER)
        if payload is None:
            raise PublisherError("repair issue candidate lost its machine payload")
        self._validate_repair_payload(payload, lineage=lineage)
        resolved_body = str(issue.get("body") or "") + f"\n\nResolved by verified PASS run {run.run_id} on `{run.sha}`.\n"
        self.api.update_issue(number, resolved_body, state="closed")
        self.api.set_labels(number, ["type:repair", "status:done"])
        return number

    def _verified_attempt_history(self, listed: Mapping[str, Any]) -> tuple[VerifiedRun, ...]:
        """以 attempt-specific API 重建單一 run ID 的完整歷史。"""

        run_id = _positive_int(listed.get("id"), "workflow run id")
        listed_sha = _require_sha(listed.get("head_sha"), "workflow run head_sha")
        latest_attempt = _positive_int(listed.get("run_attempt"), "workflow run attempt")
        values: list[VerifiedRun] = []
        for attempt in range(1, latest_attempt + 1):
            try:
                verified = self._verify_run(run_id, attempt)
            except ApiError as exc:
                raise PublisherError(
                    f"run {run_id} attempt {attempt} history is unavailable; retry accounting is blocked",
                    category=exc.category,
                ) from exc
            if verified.sha != listed_sha:
                raise PublisherError(
                    f"run {run_id} attempt {attempt} changed its source SHA during history verification"
                )
            values.append(verified)
        return tuple(values)

    def _latest_verified_run_for_sha(self, current: VerifiedRun) -> VerifiedRun:
        """重新驗證 exact SHA 的 run attempts，避免 delayed delivery 產生反向 mutation。"""

        candidates: dict[tuple[int, int], VerifiedRun] = {
            (current.run_id, current.run_attempt): current
        }
        for listed in self.api.workflow_runs(current.workflow_id):
            sha = listed.get("head_sha")
            if isinstance(sha, str) and sha.lower() == current.sha:
                for verified in self._verified_attempt_history(listed):
                    candidates[(verified.run_id, verified.run_attempt)] = verified
        return max(
            candidates.values(),
            key=lambda value: (value.run_attempt, value.run_id),
        )

    def _retry_state(
        self,
        current: VerifiedRun,
        registration: Registration,
        pulls: Mapping[int, VerifiedPull],
    ) -> tuple[int, int]:
        runs = self.api.workflow_runs(current.workflow_id)
        candidates: dict[str, list[VerifiedRun]] = {pull.merge_sha: [] for pull in pulls.values()}
        for listed in runs:
            sha = listed.get("head_sha")
            if isinstance(sha, str) and sha.lower() in candidates:
                for verified in self._verified_attempt_history(listed):
                    candidates[verified.sha].append(verified)
        candidates.setdefault(current.sha, []).append(current)
        quality_by_sha: dict[str, str | None] = {}
        infra = 0
        for sha, values in candidates.items():
            unique_values = {
                (value.run_id, value.run_attempt): value
                for value in values
            }
            # 同一 SHA 的 infra rerun 只作獨立 evidence；retry quality
            # 取最新的 conclusive PASS/code_failure，避免 infra 蓋掉 code failure。
            conclusive = [
                value
                for value in unique_values.values()
                if value.quality in {"pass", "code_failure"}
            ]
            if conclusive:
                latest_conclusive = max(
                    conclusive,
                    key=lambda value: (value.run_attempt, value.run_id),
                )
                quality_by_sha[sha] = latest_conclusive.quality
            else:
                quality_by_sha[sha] = None
            infra += sum(
                value.quality == "infrastructure_failure"
                for value in unique_values.values()
            )
        failures = 0
        for attempt in reversed(registration.attempts):
            quality = quality_by_sha.get(pulls[attempt.pr_number].merge_sha)
            if quality == "code_failure":
                failures += 1
                continue
            if quality == "pass":
                break
            # 未知或 infrastructure 結果不代表成功，也不代表新的 code
            # failure；略過它，繼續追溯較早的 lineage。只有完整 PASS
            # 才能停止計數並重置 consecutive code failures。
            if quality in {None, "infrastructure_failure"}:
                continue
            infra += 1
        return failures, infra

    def reconcile(self, run_id: int, run_attempt: int | None = None) -> PublisherResult:
        source_run = self._verify_run(run_id, run_attempt)
        source_pull = self._verified_pull_for_sha(source_run.sha)
        (
            source_registration,
            current_registration,
            pulls,
            lineage,
            protected_tip,
        ) = self._registered_context(source_run, source_pull)
        latest_source_run = self._latest_verified_run_for_sha(source_run)
        latest_attempt = current_registration.attempts[-1]
        latest_pull = pulls[latest_attempt.pr_number]
        latest_is_source = latest_pull.number == source_pull.number and latest_pull.merge_sha == source_run.sha
        if (latest_source_run.run_id, latest_source_run.run_attempt) != (
            source_run.run_id,
            source_run.run_attempt,
        ):
            failures, infrastructure = self._retry_state(
                latest_source_run,
                current_registration,
                pulls,
            )
            return PublisherResult(
                status="historical_pass" if source_run.quality == "pass" else "historical_failure",
                action="no_mutation_stale_result",
                source_run_id=source_run.run_id,
                source_run_attempt=source_run.run_attempt,
                source_sha=source_run.sha,
                source_pr_number=source_pull.number,
                latest_pr_number=latest_pull.number,
                lineage_id=lineage,
                repair_issue_number=None,
                created_repair_issue=False,
                consecutive_code_failures=failures,
                infrastructure_failures=infrastructure,
                evidence={
                    "reason": "a newer verified run attempt for the exact source SHA is authoritative",
                    "authoritative_run_id": latest_source_run.run_id,
                    "authoritative_run_attempt": latest_source_run.run_attempt,
                    "authoritative_quality": latest_source_run.quality,
                },
            )
        if source_run.quality == "pass":
            if latest_is_source:
                failures = 0
                infrastructure = 0
            else:
                failures, infrastructure = self._retry_state(
                    latest_source_run,
                    current_registration,
                    pulls,
                )
            with self._mutation_lock:
                (
                    fresh_run,
                    fresh_pull,
                    fresh_source_registration,
                    fresh_current_registration,
                    fresh_pulls,
                    fresh_lineage,
                    fresh_tip,
                ) = self._revalidate_before_mutation(
                    source_run,
                    source_pull,
                    source_registration,
                    current_registration,
                    pulls,
                    lineage,
                    protected_tip,
                )
                self._publish_pr_pass_locked(fresh_pull, fresh_run, fresh_lineage)
                issue_number = None
                if latest_is_source:
                    (
                        fresh_run,
                        fresh_pull,
                        fresh_source_registration,
                        fresh_current_registration,
                        fresh_pulls,
                        fresh_lineage,
                        fresh_tip,
                    ) = self._revalidate_before_mutation(
                        fresh_run,
                        fresh_pull,
                        fresh_source_registration,
                        fresh_current_registration,
                        fresh_pulls,
                        fresh_lineage,
                        fresh_tip,
                        require_latest_source=True,
                    )
                    issue_number = self._close_repair_if_current_locked(fresh_lineage, fresh_run)
            action = (
                "layer_b_required"
                if latest_is_source and "layer_b" in (current_registration.completion_profile or "")
                else "done_candidate"
                if latest_is_source
                else "historical_pass"
            )
            return PublisherResult(
                status="pass",
                action=action,
                source_run_id=source_run.run_id,
                source_run_attempt=source_run.run_attempt,
                source_sha=source_run.sha,
                source_pr_number=source_pull.number,
                latest_pr_number=latest_pull.number,
                lineage_id=lineage,
                repair_issue_number=issue_number,
                created_repair_issue=False,
                consecutive_code_failures=failures,
                infrastructure_failures=infrastructure,
                evidence={
                    "repository": self.config.repository,
                    "ticket_id": self._ticket_id(),
                    "completion_profile": current_registration.completion_profile,
                    "layer_b_required": "layer_b" in (current_registration.completion_profile or ""),
                    "workflow_id": source_run.workflow_id,
                    "workflow_path": source_run.workflow_path,
                    "run_url": source_run.url,
                    "check_suite_id": source_run.check_suite_id,
                    "provider": source_run.provider,
                    "required_jobs": dict(source_run.job_conclusions),
                    "finalizer_run_id": self.config.finalizer_run_id,
                    "finalizer_run_url": self.config.finalizer_run_url,
                },
            )
        if source_run.quality == "infrastructure_failure":
            failures, infrastructure = self._retry_state(source_run, current_registration, pulls)
            return PublisherResult(
                status="blocked",
                action="infrastructure_blocked",
                source_run_id=source_run.run_id,
                source_run_attempt=source_run.run_attempt,
                source_sha=source_run.sha,
                source_pr_number=source_pull.number,
                latest_pr_number=latest_pull.number,
                lineage_id=lineage,
                repair_issue_number=None,
                created_repair_issue=False,
                consecutive_code_failures=failures,
                infrastructure_failures=infrastructure,
                evidence={
                    "reason": "required provider/job results did not establish a proven code/test failure",
                    "required_jobs": dict(source_run.job_conclusions),
                    "failed_steps": list(source_run.failed_steps),
                    "verified_lineage_code_failures": failures,
                    "verified_lineage_infrastructure_failures": infrastructure,
                },
            )
        failures, infrastructure = self._retry_state(source_run, current_registration, pulls)
        if not latest_is_source:
            return PublisherResult(
                status="historical_failure",
                action="no_mutation_stale_result",
                source_run_id=source_run.run_id,
                source_run_attempt=source_run.run_attempt,
                source_sha=source_run.sha,
                source_pr_number=source_pull.number,
                latest_pr_number=latest_pull.number,
                lineage_id=lineage,
                repair_issue_number=None,
                created_repair_issue=False,
                consecutive_code_failures=failures,
                infrastructure_failures=infrastructure,
                evidence={"reason": "a newer registered attempt is authoritative"},
            )
        with self._mutation_lock:
            (
                fresh_run,
                fresh_pull,
                fresh_source_registration,
                fresh_current_registration,
                fresh_pulls,
                fresh_lineage,
                fresh_tip,
            ) = self._revalidate_before_mutation(
                source_run,
                source_pull,
                source_registration,
                current_registration,
                pulls,
                lineage,
                protected_tip,
                require_latest_source=True,
            )
            issue_number, created = self._publish_failure_locked(
                fresh_pull,
                fresh_run,
                fresh_lineage,
                failures,
            )
        _, action, _ = _repair_projection(failures)
        return PublisherResult(
            status="failure",
            action=action,
            source_run_id=source_run.run_id,
            source_run_attempt=source_run.run_attempt,
            source_sha=source_run.sha,
            source_pr_number=source_pull.number,
            latest_pr_number=latest_pull.number,
            lineage_id=lineage,
            repair_issue_number=issue_number,
            created_repair_issue=created,
            consecutive_code_failures=failures,
            infrastructure_failures=infrastructure,
            evidence={
                "repository": self.config.repository,
                "workflow_id": source_run.workflow_id,
                "workflow_path": source_run.workflow_path,
                "run_url": source_run.url,
                "check_suite_id": source_run.check_suite_id,
                "provider": source_run.provider,
                "required_jobs": dict(source_run.job_conclusions),
                "failed_steps": list(source_run.failed_steps),
            },
        )

    def reconcile_all(
        self,
        preferred_run_id: int | None = None,
        preferred_run_attempt: int | None = None,
    ) -> list[PublisherResult]:
        """Reconcile every registered attempt visible from protected develop.

        A ``workflow_run`` delivery may be replaced by a newer delivery while
        it is pending.  The finalizer therefore discovers all completed
        producer runs for the registered merged SHAs instead of trusting only
        the event that woke it up.
        """

        workflow_id = self._trusted_workflow_id()
        protected_tip = self.api.branch_tip("develop")
        protected_manifest = self.api.manifest(self.config.manifest_path, protected_tip)
        if self.config.ticket_id is None:
            registrations = registered_registrations(protected_manifest)
        else:
            self._resolved_ticket_id = self.config.ticket_id
            registration = parse_registration(protected_manifest, self._ticket_id())
            registrations = {self._ticket_id(): registration}
        if not registrations:
            raise PublisherError("protected manifest has no registered post-merge ticket")
        registered_shas: dict[str, int] = {}
        order: dict[str, int] = {}
        order_index = 0
        for registration in registrations.values():
            previous_sha: str | None = None
            for attempt in registration.attempts:
                pull = self.api.pull(attempt.pr_number)
                base = pull.get("base")
                base_repo = base.get("repo") if isinstance(base, dict) else None
                if (
                    not pull.get("merged_at")
                    or not isinstance(base, dict)
                    or base.get("ref") != "develop"
                    or not isinstance(base_repo, dict)
                    or str(base_repo.get("full_name", "")).lower() != self.config.repository.lower()
                ):
                    raise PublisherError(
                        f"registered PR #{attempt.pr_number} is not a merged develop PR"
                    )
                sha = _require_sha(
                    pull.get("merge_commit_sha"),
                    f"PR #{attempt.pr_number} merge_commit_sha",
                )
                if previous_sha and self.api.compare(previous_sha, sha).get("status") not in {
                    "ahead",
                    "identical",
                }:
                    raise PublisherError("registered PR ancestry is not monotonic")
                previous_sha = sha
                if sha in registered_shas:
                    raise PublisherError("a protected source SHA is registered to multiple tickets")
                registered_shas[sha] = attempt.pr_number
                order[sha] = order_index
                order_index += 1

        listed_runs = self.api.workflow_runs(workflow_id)
        candidates: dict[tuple[int, int | None], tuple[int, int | None, str]] = {}
        for listed in listed_runs:
            run_id = listed.get("id")
            sha = listed.get("head_sha")
            if not isinstance(run_id, int) or isinstance(run_id, bool) or not isinstance(sha, str):
                continue
            sha = sha.lower()
            if sha not in registered_shas:
                continue
            attempt = listed.get("run_attempt")
            if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt <= 0:
                attempt = None
            candidates[(run_id, attempt)] = (order[sha], attempt, sha)

        if preferred_run_id is not None:
            preferred = self.api.run(preferred_run_id)
            preferred_sha = _require_sha(preferred.get("head_sha"), "preferred source run head_sha")
            if preferred_sha not in registered_shas:
                raise PublisherError("preferred source run is not a registered develop attempt")
            preferred_attempt = preferred.get("run_attempt")
            if not isinstance(preferred_attempt, int) or isinstance(preferred_attempt, bool):
                preferred_attempt = None
            candidates[(preferred_run_id, preferred_attempt)] = (
                order[preferred_sha],
                preferred_attempt,
                preferred_sha,
            )

        if not candidates:
            raise PublisherError("no completed producer run matches a registered develop attempt")

        ordered = sorted(
            candidates,
            key=lambda key: (
                candidates[key][0],
                candidates[key][1] or 0,
                key[0],
            ),
        )
        results: list[PublisherResult] = []
        for run_id, attempt in ordered:
            expected_attempt = preferred_run_attempt if run_id == preferred_run_id else attempt
            results.append(self.reconcile(run_id, expected_attempt))
        return results


@dataclass(frozen=True)
class SimulationResult:
    case: str
    issue_number: int
    created_issue: bool
    consecutive_code_failures: int
    observations: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "simulation",
            "namespace": SIMULATION_NAMESPACE,
            "case": self.case,
            "issue_number": self.issue_number,
            "created_issue": self.created_issue,
            "consecutive_code_failures": self.consecutive_code_failures,
            "observations": list(self.observations),
            "production_ticket_mutated": False,
        }


class SimulationPublisher:
    """只使用 Issues API、且與 production lineage state 隔離的 simulation。"""

    _title = "[W11-010 simulation] isolated post-merge reducer"

    def __init__(self, api: GithubApi, config: PublisherConfig) -> None:
        self.api = api
        self.config = config
        self.ticket_id = config.ticket_id or "W11-010"

    def _trusted_author(self, value: Mapping[str, Any]) -> bool:
        user = value.get("user")
        return isinstance(user, dict) and user.get("type") == "Bot" and user.get("login") in self.config.publisher_logins

    def _simulation_sha(self) -> str:
        return hashlib.sha256(
            f"{self.config.repository.lower()}|{self.ticket_id}|{SIMULATION_NAMESPACE}".encode("utf-8")
        ).hexdigest()[:40]

    def _validate_payload(self, payload: Mapping[str, Any]) -> None:
        if (
            payload.get("kind") != "w11-010-simulation"
            or payload.get("version") != 1
            or payload.get("repository") != self.config.repository
            or payload.get("ticket_id") != self.ticket_id
            or payload.get("namespace") != SIMULATION_NAMESPACE
            or payload.get("simulated") is not True
        ):
            raise PublisherError("simulation projection has an invalid protected schema")
        if payload.get("case") not in SIMULATION_CASES:
            raise PublisherError("simulation projection has an invalid case")
        if _require_sha(payload.get("source_sha"), "simulation source_sha") != self._simulation_sha():
            raise PublisherError("simulation projection source_sha is invalid")
        failures = payload.get("consecutive_code_failures")
        if isinstance(failures, bool) or not isinstance(failures, int) or failures not in (0, 1):
            raise PublisherError("simulation projection retry count is invalid")
        observations = payload.get("observations")
        if not isinstance(observations, list) or not observations or not all(
            isinstance(value, str) and value in SIMULATION_CASES for value in observations
        ):
            raise PublisherError("simulation projection observations are invalid")

    def _find_issue(self) -> tuple[int, Mapping[str, Any]] | None:
        matches: list[tuple[int, Mapping[str, Any]]] = []
        for issue in self.api.issues():
            payload = _parse_machine_payload(issue.get("body"), SIMULATION_MARKER)
            if payload is None:
                continue
            if not self._trusted_author(issue):
                raise PublisherError("simulation marker belongs to an untrusted author")
            self._validate_payload(payload)
            number = _positive_int(issue.get("number"), "simulation issue number")
            matches.append((number, issue))
        if len(matches) > 1:
            raise PublisherError("multiple simulation issues exist for the fixed namespace")
        return matches[0] if matches else None

    def _body(
        self,
        *,
        case: str,
        observations: Sequence[str],
        failures: int,
    ) -> str:
        payload = {
            "kind": "w11-010-simulation",
            "version": 1,
            "repository": self.config.repository,
            "ticket_id": self.ticket_id,
            "namespace": SIMULATION_NAMESPACE,
            "simulated": True,
            "case": case,
            "source_sha": self._simulation_sha(),
            "consecutive_code_failures": failures,
            "observations": list(observations),
        }
        return "\n".join(
            [
                SIMULATION_MARKER,
                "```json",
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                "```",
                "",
                "This issue is an isolated W11-010 simulation projection.",
                "It must never change production ticket state, retry state, or manifest bookkeeping.",
            ]
        )

    def reconcile(self, case: str) -> SimulationResult:
        if case not in SIMULATION_CASES:
            raise PublisherError(f"simulation case must be one of: {', '.join(SIMULATION_CASES)}")
        if self.config.repository.lower() != CANONICAL_REPOSITORY.lower():
            raise PublisherError("simulation requires the canonical Windows repository")
        repository = self.api.repository_info()
        if str(repository.get("full_name", "")).lower() != self.config.repository.lower():
            raise PublisherError("GitHub repository identity does not match the trusted repository")
        with PostMergePublisher._mutation_lock:
            found = self._find_issue()
            existing_payload = _parse_machine_payload(found[1].get("body"), SIMULATION_MARKER) if found else None
            if existing_payload is not None:
                self._validate_payload(existing_payload)
            if case == "rerun":
                if found is None or existing_payload is None or existing_payload.get("case") not in {"fail", "rerun"}:
                    raise PublisherError("simulation rerun requires an existing simulated failure")
                observations = list(existing_payload["observations"])
                if observations[-1] != "rerun":
                    observations.append("rerun")
                failures = 1
            elif case == "fail":
                observations = list(existing_payload["observations"]) if existing_payload else []
                if not observations or observations[-1] != "fail":
                    observations.append("fail")
                failures = 1
            else:
                observations = list(existing_payload["observations"]) if existing_payload else []
                if not observations or observations[-1] != "pass":
                    observations.append("pass")
                failures = 0
            body = self._body(case=case, observations=observations, failures=failures)
            state = "closed" if case == "pass" else "open"
            created = False
            if found is None:
                created_value = self.api.create_issue(self._title, body, [])
                number = _positive_int(created_value.get("number"), "created simulation issue number")
                if not self._trusted_author(created_value):
                    raise PublisherError("created simulation issue was not authored by the trusted bot")
                created_payload = _parse_machine_payload(created_value.get("body"), SIMULATION_MARKER)
                if created_payload is None:
                    raise PublisherError("created simulation issue lost its machine payload")
                self._validate_payload(created_payload)
                created = True
                if state == "closed":
                    updated = self.api.update_issue(number, body, state=state)
                    if not self._trusted_author(updated):
                        raise PublisherError("updated simulation issue was not authored by the trusted bot")
                    updated_payload = _parse_machine_payload(updated.get("body"), SIMULATION_MARKER)
                    if updated_payload is None:
                        raise PublisherError("updated simulation issue lost its machine payload")
                    self._validate_payload(updated_payload)
            else:
                number = found[0]
                updated = self.api.update_issue(number, body, state=state)
                if not self._trusted_author(updated):
                    raise PublisherError("updated simulation issue was not authored by the trusted bot")
                updated_payload = _parse_machine_payload(updated.get("body"), SIMULATION_MARKER)
                if updated_payload is None:
                    raise PublisherError("updated simulation issue lost its machine payload")
                self._validate_payload(updated_payload)
            return SimulationResult(case, number, created, failures, tuple(observations))


def _is_actions_suite(suite: Mapping[str, Any]) -> bool:
    app = suite.get("app")
    if not isinstance(app, dict):
        return False
    slug = str(app.get("slug") or "").lower()
    name = str(app.get("name") or "").lower()
    return slug in {"github-actions", "github-actions[bot]"} or name == "github actions"


def _parse_int(value: str | None, label: str) -> int | None:
    if value is None or not value.strip():
        return None
    if not value.isdigit() or int(value) <= 0:
        raise PublisherError(f"{label} must be a positive integer")
    return int(value)


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--run-id", default=os.environ.get("POST_MERGE_RUN_ID"))
    parser.add_argument("--run-attempt", default=os.environ.get("POST_MERGE_RUN_ATTEMPT"))
    parser.add_argument(
        "--simulation-case",
        choices=("none", *SIMULATION_CASES),
        default=os.environ.get("POST_MERGE_SIMULATION_CASE") or None,
        help="執行固定的 W11-010 Issues API simulation namespace；不得進行 production reconciliation。",
    )
    parser.add_argument("--finalizer-run-id", default=os.environ.get("GITHUB_RUN_ID"))
    parser.add_argument("--finalizer-run-url", default=os.environ.get("GITHUB_SERVER_URL", "") + "/" + os.environ.get("GITHUB_REPOSITORY", "") + "/actions/runs/" + os.environ.get("GITHUB_RUN_ID", ""))
    args = parser.parse_args()
    try:
        if not args.repo:
            raise PublisherError("GITHUB_REPOSITORY is required")
        finalizer_id = _parse_int(args.finalizer_run_id, "GITHUB_RUN_ID")
        simulation_case = None if args.simulation_case in {None, "", "none"} else args.simulation_case
        if simulation_case is not None:
            if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
                raise PublisherError("simulation requires a workflow_dispatch event")
            if os.environ.get("GITHUB_REF") != "refs/heads/develop":
                raise PublisherError("simulation requires refs/heads/develop")
            simulation_publisher = SimulationPublisher(
                GithubApi(UrllibTransport(os.environ.get("GITHUB_TOKEN", "")), args.repo),
                PublisherConfig(
                    repository=args.repo,
                    finalizer_run_id=finalizer_id,
                    finalizer_run_url=args.finalizer_run_url or None,
                ),
            )
            print(json.dumps(simulation_publisher.reconcile(simulation_case).as_dict(), sort_keys=True))
            return 0
        run_id = _parse_int(args.run_id, "POST_MERGE_RUN_ID")
        run_attempt = _parse_int(args.run_attempt, "POST_MERGE_RUN_ATTEMPT")
        publisher = PostMergePublisher(
            GithubApi(UrllibTransport(os.environ.get("GITHUB_TOKEN", "")), args.repo),
            PublisherConfig(
                repository=args.repo,
                finalizer_run_id=finalizer_id,
                finalizer_run_url=args.finalizer_run_url or None,
            ),
        )
        results = publisher.reconcile_all(run_id, run_attempt)
        if len(results) == 1:
            print(json.dumps(results[0].as_dict(), sort_keys=True))
        else:
            print(
                json.dumps(
                    {"status": "reconciled", "results": [result.as_dict() for result in results]},
                    sort_keys=True,
                )
            )
        return 0
    except PublisherError as exc:
        print(json.dumps({"status": "blocked", "category": exc.category, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
