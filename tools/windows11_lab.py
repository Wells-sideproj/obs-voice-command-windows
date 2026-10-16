"""Offline safety contracts for the Windows 11 OBS certification lab.

This module deliberately does not install anything, start OBS, or mutate a
runner.  It parses a complete, non-secret lab fixture and provides the small
guards that the future live harness needs: an isolated portable lab root, a
project-local frozen runtime, a loopback-only endpoint, listener ownership,
and a process-local mutex.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence

import tomllib

from obs_voice_command.config import ZoomConfig


LAB_SCHEMA_VERSION = 1
EXPECTED_UV_VERSION = "0.11.8"
LOOPBACK_HOST = "127.0.0.1"
OBS_PORT = 4455

_SENSITIVE_KEY_NAMES = frozenset(
    {
        "pass",
        "password",
        "passwd",
        "secret",
        "token",
        "pat",
        "managedpat",
        "credential",
        "authorization",
        "apikey",
        "privatekey",
        "cookie",
    }
)

_LAB_CONFIGURATION_CODES = frozenset(
    {
        "SECTION_NOT_TABLE",
        "FIELD_REQUIRED",
        "PATH_NOT_ABSOLUTE",
        "NUMBER_INVALID",
        "NUMBER_NOT_FINITE",
        "PORT_INVALID",
        "SECRET_FIELD_FORBIDDEN",
        "FIXTURE_MISSING",
        "FIXTURE_UNREADABLE",
        "ROOT_NOT_TABLE",
        "SCHEMA_UNSUPPORTED",
        "LAB_ROOT_NOT_ISOLATED",
        "OBS_PATH_OUTSIDE_LAB",
        "OBS_PATH_NOT_PORTABLE_LAYOUT",
        "OBS_NOT_PORTABLE",
        "ENDPOINT_HOST_FORBIDDEN",
        "ENDPOINT_PORT_FORBIDDEN",
        "UV_VERSION_UNSUPPORTED",
        "RUNTIME_PATH_OUTSIDE_LAB",
        "LOCKFILE_PATH_INVALID",
        "ZOOM_LEVEL_INVALID",
        "ZOOM_DEADZONE_INVALID",
        "ZOOM_SMOOTHING_INVALID",
        "LAB_PATHS_NOT_READY",
    }
)
_LAB_SAFETY_CODES = frozenset(
    {
        "LISTENER_QUERY_UNKNOWN",
        "LISTENER_DATA_INVALID",
        "LISTENER_RECORD_INVALID",
        "LISTENER_FIELDS_MISSING",
        "LISTENER_FIELDS_INVALID",
        "LISTENER_PLATFORM_UNSUPPORTED",
        "LISTENER_QUERY_FAILED",
        "ENDPOINT_HOST_FORBIDDEN",
        "ENDPOINT_PORT_FORBIDDEN",
        "ENDPOINT_PROBE_FAILED",
        "LISTENER_EXPOSED",
        "ENDPOINT_OCCUPIED",
        "PROCESS_PID_INVALID",
        "LISTENER_OWNER_UNKNOWN",
        "LISTENER_OWNER_MISMATCH",
        "LAB_MUTEX_UNAVAILABLE",
        "LAB_ROOT_NOT_ABSOLUTE",
    }
)


def _safe_error_code(error: BaseException, allowed: frozenset[str], fallback: str) -> str:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and code in allowed else fallback


def _is_sensitive_key(key: str) -> bool:
    words = [word.lower() for word in re.split(r"[^A-Za-z0-9]+", key) if word]
    normalised = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalised in _SENSITIVE_KEY_NAMES or any(
        word in _SENSITIVE_KEY_NAMES for word in words
    )


class LabConfigurationError(ValueError):
    """The lab fixture cannot be used safely."""

    def __init__(self, message: str, *, code: str = "INVALID_FIXTURE") -> None:
        super().__init__(message)
        self.code = code


class LabSafetyError(RuntimeError):
    """A live-lab safety guard could not prove its required invariant."""

    def __init__(self, message: str, *, code: str = "SAFETY_GUARD_FAILED") -> None:
        super().__init__(message)
        self.code = code


def _table(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LabConfigurationError(
            f"fixture section [{name}] must be a table", code="SECTION_NOT_TABLE"
        )
    return value


def _required_text(table: Mapping[str, Any], key: str, section: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LabConfigurationError(
            f"fixture field [{section}].{key} is required", code="FIELD_REQUIRED"
        )
    return value.strip()


def _required_absolute_path(
    table: Mapping[str, Any], key: str, section: str
) -> Path:
    value = _required_text(table, key, section)
    path = Path(value)
    if not path.is_absolute():
        raise LabConfigurationError(
            f"fixture field [{section}].{key} must be an absolute path",
            code="PATH_NOT_ABSOLUTE",
        )
    return path.resolve(strict=False)


def _required_number(
    table: Mapping[str, Any], key: str, section: str, *, positive: bool = False
) -> float:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LabConfigurationError(
            f"fixture field [{section}].{key} must be numeric", code="NUMBER_INVALID"
        )
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        raise LabConfigurationError(
            f"fixture field [{section}].{key} must be finite and positive"
            if positive
            else f"fixture field [{section}].{key} must be finite",
            code="NUMBER_NOT_FINITE",
        )
    return number


def _required_port(table: Mapping[str, Any], key: str, section: str) -> int:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise LabConfigurationError(
            f"fixture field [{section}].{key} must be an integer", code="PORT_INVALID"
        )
    return value


def _is_within(child: Path, parent: Path) -> bool:
    return child == parent or child.is_relative_to(parent)


def _reject_sensitive_values(value: Any, path: str = "fixture") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                raise LabConfigurationError(
                    f"{path}.{key_text} is forbidden; provide secrets only to the live step",
                    code="SECRET_FIELD_FORBIDDEN",
                )
            _reject_sensitive_values(child, f"{path}.{key_text}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_sensitive_values(child, f"{path}[{index}]")


@dataclass(frozen=True)
class LabRuntime:
    """Absolute, project-local runtime paths prepared outside this ticket."""

    python_path: Path
    uv_path: Path
    uv_version: str
    project_venv: Path
    cache_dir: Path
    lockfile: Path


@dataclass(frozen=True)
class ObsFixture:
    """The explicit portable OBS fixture selected by a certification operator."""

    executable: Path
    lab_root: Path
    portable: bool
    profile: str
    collection: str
    host: str
    port: int
    scene: str
    source: str


@dataclass(frozen=True)
class Windows11Lab:
    """Validated, non-secret offline lab description."""

    fixture_path: Path
    obs: ObsFixture
    runtime: LabRuntime
    zoom: ZoomConfig
    structural_valid: bool = True
    runtime_verified: bool = False

    @property
    def mutex_path(self) -> Path:
        return self.obs.lab_root / ".w11-obs-integration.lock"

    def public_summary(self) -> dict[str, Any]:
        """Return only whitelisted fixture data for CI summaries."""

        return {
            "status": "structural_valid",
            "structural_valid": self.structural_valid,
            "runtime_verified": self.runtime_verified,
            "schema_version": LAB_SCHEMA_VERSION,
            "obs": {
                "portable": self.obs.portable,
                "host": self.obs.host,
                "port": self.obs.port,
            },
            "runtime": {
                "uv_version": self.runtime.uv_version,
            },
            "zoom": {
                "level": self.zoom.level,
                "deadzone": self.zoom.deadzone,
                "smoothing": self.zoom.smoothing,
            },
        }


def load_lab_fixture(
    path: str | os.PathLike[str], *, repo_root: str | os.PathLike[str] | None = None
) -> Windows11Lab:
    """Parse and structurally validate a complete lab fixture.

    This function performs no filesystem creation and does not require the
    portable executable or prepared runtime to exist.  Use
    :func:`validate_lab_fixture` for the live preflight existence checks.
    """

    fixture_path = Path(path).resolve(strict=False)
    if not fixture_path.is_file():
        raise LabConfigurationError("lab fixture does not exist", code="FIXTURE_MISSING")
    try:
        with fixture_path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        del exc
        raise LabConfigurationError("unable to read lab fixture", code="FIXTURE_UNREADABLE") from None
    if not isinstance(data, Mapping):
        raise LabConfigurationError("lab fixture root must be a TOML table", code="ROOT_NOT_TABLE")
    _reject_sensitive_values(data)

    schema_version = data.get("schema_version")
    if schema_version != LAB_SCHEMA_VERSION:
        raise LabConfigurationError(
            f"unsupported lab fixture schema; expected {LAB_SCHEMA_VERSION}",
            code="SCHEMA_UNSUPPORTED",
        )

    lab_data = _table(data.get("lab"), "lab")
    lab_root = _required_absolute_path(lab_data, "lab_root", "lab")
    if repo_root is not None:
        repository = Path(repo_root).resolve(strict=False)
        if _is_within(lab_root, repository) or _is_within(repository, lab_root):
            raise LabConfigurationError(
                "lab_root must be independent from the checked-out repository",
                code="LAB_ROOT_NOT_ISOLATED",
            )

    obs_data = _table(data.get("obs"), "obs")
    executable = _required_absolute_path(obs_data, "executable", "obs")
    if not _is_within(executable, lab_root):
        raise LabConfigurationError(
            "portable OBS executable must live under lab_root", code="OBS_PATH_OUTSIDE_LAB"
        )
    if executable.name.lower() != "obs64.exe" or executable.parent.name.lower() != "64bit":
        raise LabConfigurationError(
            "portable OBS executable must be obs/bin/64bit/obs64.exe",
            code="OBS_PATH_NOT_PORTABLE_LAYOUT",
        )
    if obs_data.get("portable") is not True:
        raise LabConfigurationError("[obs].portable must be true", code="OBS_NOT_PORTABLE")
    profile = _required_text(obs_data, "profile", "obs")
    collection = _required_text(obs_data, "collection", "obs")
    scene = _required_text(obs_data, "scene", "obs")
    source = _required_text(obs_data, "source", "obs")
    host = _required_text(obs_data, "host", "obs")
    if host != LOOPBACK_HOST:
        raise LabConfigurationError(
            "[obs].host is hard-locked to 127.0.0.1", code="ENDPOINT_HOST_FORBIDDEN"
        )
    port = _required_port(obs_data, "port", "obs")
    if port != OBS_PORT:
        raise LabConfigurationError(
            "[obs].port is hard-locked to 4455", code="ENDPOINT_PORT_FORBIDDEN"
        )

    runtime_data = _table(data.get("runtime"), "runtime")
    runtime = LabRuntime(
        python_path=_required_absolute_path(runtime_data, "python_path", "runtime"),
        uv_path=_required_absolute_path(runtime_data, "uv_path", "runtime"),
        uv_version=_required_text(runtime_data, "uv_version", "runtime"),
        project_venv=_required_absolute_path(runtime_data, "project_venv", "runtime"),
        cache_dir=_required_absolute_path(runtime_data, "cache_dir", "runtime"),
        lockfile=_required_absolute_path(runtime_data, "lockfile", "runtime"),
    )
    if runtime.uv_version != EXPECTED_UV_VERSION:
        raise LabConfigurationError(
            f"[runtime].uv_version must be {EXPECTED_UV_VERSION}",
            code="UV_VERSION_UNSUPPORTED",
        )
    for name, child in (
        ("python_path", runtime.python_path),
        ("uv_path", runtime.uv_path),
        ("project_venv", runtime.project_venv),
        ("cache_dir", runtime.cache_dir),
    ):
        if not _is_within(child, lab_root):
            raise LabConfigurationError(
                f"[runtime].{name} must be under lab_root", code="RUNTIME_PATH_OUTSIDE_LAB"
            )
    if not (_is_within(runtime.lockfile, lab_root) or (repo_root and _is_within(runtime.lockfile, Path(repo_root).resolve(strict=False)))):
        raise LabConfigurationError(
            "[runtime].lockfile must be under lab_root or the checked-out repository",
            code="LOCKFILE_PATH_INVALID",
        )

    zoom_data = _table(data.get("zoom"), "zoom")
    zoom = ZoomConfig(
        level=_required_number(zoom_data, "level", "zoom", positive=True),
        deadzone=_required_number(zoom_data, "deadzone", "zoom"),
        smoothing=_required_number(zoom_data, "smoothing", "zoom"),
    )
    if zoom.level <= 1.0:
        raise LabConfigurationError(
            "[zoom].level must be greater than 1.0", code="ZOOM_LEVEL_INVALID"
        )
    if not 0.0 <= zoom.deadzone < 1.0:
        raise LabConfigurationError(
            "[zoom].deadzone must be in [0, 1)", code="ZOOM_DEADZONE_INVALID"
        )
    if not 0.0 < zoom.smoothing <= 1.0:
        raise LabConfigurationError(
            "[zoom].smoothing must be in (0, 1]", code="ZOOM_SMOOTHING_INVALID"
        )

    return Windows11Lab(
        fixture_path=fixture_path,
        obs=ObsFixture(
            executable=executable,
            lab_root=lab_root,
            portable=True,
            profile=profile,
            collection=collection,
            host=host,
            port=port,
            scene=scene,
            source=source,
        ),
        runtime=runtime,
        zoom=zoom,
    )


def validate_lab_fixture(
    path: str | os.PathLike[str],
    *,
    repo_root: str | os.PathLike[str] | None = None,
    require_existing: bool = True,
) -> Windows11Lab:
    """Validate the prepared paths without creating or installing anything."""

    lab = load_lab_fixture(path, repo_root=repo_root)
    if not require_existing:
        return lab

    missing: list[str] = []
    for label, candidate in (
        ("lab_root", lab.obs.lab_root),
        ("OBS executable", lab.obs.executable),
        ("Python 3.12", lab.runtime.python_path),
        ("uv", lab.runtime.uv_path),
        ("project venv", lab.runtime.project_venv),
        ("uv cache", lab.runtime.cache_dir),
        ("uv.lock", lab.runtime.lockfile),
    ):
        if not candidate.exists():
            missing.append(f"{label}={candidate}")
    if not lab.obs.lab_root.is_dir():
        missing.append(f"lab_root is not a directory={lab.obs.lab_root}")
    for label, candidate in (
        ("OBS executable", lab.obs.executable),
        ("Python 3.12", lab.runtime.python_path),
        ("uv", lab.runtime.uv_path),
        ("uv.lock", lab.runtime.lockfile),
    ):
        if candidate.exists() and not candidate.is_file():
            missing.append(f"{label} is not a file={candidate}")
    for label, candidate in (
        ("project venv", lab.runtime.project_venv),
        ("uv cache", lab.runtime.cache_dir),
    ):
        if candidate.exists() and not candidate.is_dir():
            missing.append(f"{label} is not a directory={candidate}")
    if missing:
        raise LabConfigurationError(
            "offline lab preparation is incomplete",
            code="LAB_PATHS_NOT_READY",
        )
    return lab


@dataclass(frozen=True)
class TcpListener:
    local_address: str
    port: int
    owning_pid: int


class ListenerInspector(Protocol):
    def __call__(self, port: int) -> Sequence[TcpListener]: ...


def _parse_listener_json(raw: str, port: int) -> list[TcpListener]:
    if not isinstance(raw, str) or not raw.strip():
        raise LabSafetyError(
            "listener inspection returned no result",
            code="LISTENER_QUERY_UNKNOWN",
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise LabSafetyError(
            "listener inspection returned invalid data",
            code="LISTENER_DATA_INVALID",
        ) from None
    if payload == []:
        return []
    if isinstance(payload, Mapping):
        records = [payload]
    elif isinstance(payload, list):
        records = payload
    else:
        raise LabSafetyError(
            "listener inspection returned an unknown result",
            code="LISTENER_QUERY_UNKNOWN",
        )
    listeners: list[TcpListener] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise LabSafetyError(
                "listener inspection returned an invalid record",
                code="LISTENER_RECORD_INVALID",
            )
        try:
            address = record["LocalAddress"]
            local_port = record["LocalPort"]
            owning_pid = record["OwningProcess"]
        except KeyError:
            raise LabSafetyError(
                "listener inspection omitted required fields",
                code="LISTENER_FIELDS_MISSING",
            ) from None
        if (
            not isinstance(address, str)
            or isinstance(local_port, bool)
            or not isinstance(local_port, int)
            or isinstance(owning_pid, bool)
            or not isinstance(owning_pid, int)
            or owning_pid <= 0
        ):
            raise LabSafetyError(
                "listener inspection returned invalid fields",
                code="LISTENER_FIELDS_INVALID",
            )
        if local_port == port:
            listeners.append(TcpListener(address, local_port, owning_pid))
    return listeners


def inspect_tcp_listeners(port: int = OBS_PORT) -> list[TcpListener]:
    """Read Windows listener ownership without exposing command output."""

    if os.name != "nt":
        raise LabSafetyError(
            "listener ownership inspection is only supported on Windows",
            code="LISTENER_PLATFORM_UNSUPPORTED",
        )
    command = (
        "$listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop | "
        "Select-Object LocalAddress,LocalPort,OwningProcess); "
        "ConvertTo-Json -InputObject $listeners -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        raise LabSafetyError(
            "unable to inspect localhost listener ownership",
            code="LISTENER_QUERY_FAILED",
        ) from None
    return _parse_listener_json(completed.stdout, port)


def _validate_endpoint(host: str, port: int) -> None:
    if host != LOOPBACK_HOST:
        raise LabSafetyError(
            "OBS endpoint is hard-locked to 127.0.0.1", code="ENDPOINT_HOST_FORBIDDEN"
        )
    if port != OBS_PORT:
        raise LabSafetyError(
            "OBS endpoint port is hard-locked to 4455", code="ENDPOINT_PORT_FORBIDDEN"
        )


def _is_connection_refused(error: OSError) -> bool:
    return (
        isinstance(error, ConnectionRefusedError)
        or error.errno in {getattr(__import__("errno"), "ECONNREFUSED", 111), 61, 10061}
        or getattr(error, "winerror", None) == 10061
    )


def _assert_loopback_listeners(listeners: Sequence[TcpListener]) -> None:
    exposed = [listener for listener in listeners if listener.local_address != LOOPBACK_HOST]
    if exposed:
        raise LabSafetyError(
            "OBS port has a wildcard or non-loopback listener",
            code="LISTENER_EXPOSED",
        )


def _checked_listener_sequence(value: Sequence[TcpListener] | None) -> list[TcpListener]:
    if value is None or isinstance(value, (str, bytes, bytearray)):
        raise LabSafetyError(
            "listener inspection returned an unknown result",
            code="LISTENER_QUERY_UNKNOWN",
        )
    try:
        listeners = list(value)
    except TypeError:
        raise LabSafetyError(
            "listener inspection returned an unknown result",
            code="LISTENER_QUERY_UNKNOWN",
        ) from None
    if any(not isinstance(listener, TcpListener) for listener in listeners):
        raise LabSafetyError(
            "listener inspection returned an invalid record",
            code="LISTENER_RECORD_INVALID",
        )
    return listeners


def assert_endpoint_free(
    host: str,
    port: int,
    *,
    timeout: float = 1.0,
    connect: Callable[..., Any] = socket.create_connection,
    inspector: ListenerInspector = inspect_tcp_listeners,
) -> None:
    """Fail closed unless the loopback OBS port is provably unused."""

    _validate_endpoint(host, port)
    try:
        connection = connect((host, port), timeout=timeout)
    except OSError as exc:
        if not _is_connection_refused(exc):
            raise LabSafetyError(
                "unable to prove OBS endpoint is free", code="ENDPOINT_PROBE_FAILED"
            ) from None
        listeners = _checked_listener_sequence(inspector(port))
        _assert_loopback_listeners(listeners)
        if listeners:
            raise LabSafetyError("OBS endpoint is already occupied", code="ENDPOINT_OCCUPIED")
        return
    try:
        connection.close()
    finally:
        raise LabSafetyError("OBS endpoint is already occupied", code="ENDPOINT_OCCUPIED")


def assert_listener_owned(
    host: str,
    port: int,
    owning_pid: int,
    *,
    inspector: ListenerInspector = inspect_tcp_listeners,
) -> None:
    """Require the loopback listener to belong only to our OBS process."""

    _validate_endpoint(host, port)
    if owning_pid <= 0:
        raise LabSafetyError(
            "owned OBS process did not expose a valid PID", code="PROCESS_PID_INVALID"
        )
    listeners = _checked_listener_sequence(inspector(port))
    if not listeners:
        raise LabSafetyError(
            "OBS endpoint has no readable listener owner", code="LISTENER_OWNER_UNKNOWN"
        )
    _assert_loopback_listeners(listeners)
    owners = {listener.owning_pid for listener in listeners}
    if owners != {owning_pid}:
        raise LabSafetyError(
            "OBS endpoint listener is not owned exclusively by OBS",
            code="LISTENER_OWNER_MISMATCH",
        )


class LocalLabMutex:
    """Cross-process advisory mutex backed by a file under the lab root."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).resolve(strict=False)
        self._handle: Any | None = None

    def __enter__(self) -> "LocalLabMutex":
        try:
            self._handle = self.path.open("a+b")
            if self.path.stat().st_size == 0:
                self._handle.write(b"0")
                self._handle.flush()
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, AttributeError) as exc:
            self._close_handle()
            del exc
            raise LabSafetyError(
                "another Windows 11 OBS lab run already owns the mutex",
                code="LAB_MUTEX_UNAVAILABLE",
            ) from None
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._close_handle()

    def _close_handle(self) -> None:
        if self._handle is not None:
            with contextlib.suppress(OSError):
                self._handle.close()
            self._handle = None


def local_lab_mutex(lab_root: str | os.PathLike[str]) -> LocalLabMutex:
    root = Path(lab_root).resolve(strict=False)
    if not root.is_absolute():
        raise LabSafetyError(
            "lab_root must be absolute for the local mutex", code="LAB_ROOT_NOT_ABSOLUTE"
        )
    return LocalLabMutex(root / ".w11-obs-integration.lock")


class _InvalidArguments(Exception):
    """The CLI received arguments that cannot be safely reported verbatim."""


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise _InvalidArguments


def _cli() -> int:
    parser = _SafeArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--require-existing", action="store_true")
    try:
        args = parser.parse_args()
        lab = validate_lab_fixture(
            args.fixture,
            repo_root=args.repo_root,
            require_existing=args.require_existing,
        )
        print(json.dumps(lab.public_summary(), sort_keys=True))
        return 0
    except _InvalidArguments:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "stage": "cli",
                    "code": "INVALID_ARGUMENTS",
                },
                sort_keys=True,
            )
        )
        return 1
    except LabConfigurationError as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "stage": "fixture",
                    "code": _safe_error_code(exc, _LAB_CONFIGURATION_CODES, "INVALID_FIXTURE"),
                },
                sort_keys=True,
            )
        )
        return 1
    except LabSafetyError as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "stage": "safety",
                    "code": _safe_error_code(
                        exc, _LAB_SAFETY_CODES, "SAFETY_GUARD_FAILED"
                    ),
                },
                sort_keys=True,
            )
        )
        return 1
    except Exception:
        print(
            json.dumps(
                {"status": "blocked", "stage": "lab", "code": "LAB_CLI_FAILED"},
                sort_keys=True,
            )
        )
        return 1


__all__ = [
    "EXPECTED_UV_VERSION",
    "LAB_SCHEMA_VERSION",
    "LOOPBACK_HOST",
    "OBS_PORT",
    "LabConfigurationError",
    "LabRuntime",
    "LabSafetyError",
    "LocalLabMutex",
    "ObsFixture",
    "TcpListener",
    "Windows11Lab",
    "assert_endpoint_free",
    "assert_listener_owned",
    "inspect_tcp_listeners",
    "load_lab_fixture",
    "local_lab_mutex",
    "validate_lab_fixture",
]


if __name__ == "__main__":
    raise SystemExit(_cli())
