"""Deterministic command-to-transform harness for the deferred Layer B path.

The workflow that calls this module is intentionally inactive until W11-011.
The required command source is a direct ``ZoomController.handle`` call rather
than live speech recognition, so the production controller and an injected
OBS boundary can be tested without a microphone, ASR model, or live adapter.
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ContextManager, Protocol

from obs_voice_command.config import ZoomConfig
from obs_voice_command.platform.common import DisplayInfo
from obs_voice_command.runtime import PointerPort, ZoomController
from obs_voice_command.zoom import Transform

if __package__:
    from .windows11_lab import (
        LabSafetyError,
        TcpListener,
        assert_endpoint_free as lab_assert_endpoint_free,
        assert_listener_owned as lab_assert_listener_owned,
        inspect_tcp_listeners,
    )
else:
    from windows11_lab import (
        LabSafetyError,
        TcpListener,
        assert_endpoint_free as lab_assert_endpoint_free,
        assert_listener_owned as lab_assert_listener_owned,
        inspect_tcp_listeners,
    )


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4455
PREP_LIVE_DISABLED = "PREP_LIVE_DISABLED"

_ALLOWED_STAGES = frozenset(
    {
        "baseline",
        "cleanup",
        "endpoint",
        "harness",
        "lifecycle",
        "listener",
        "portable",
        "preparation",
        "spawn",
        "transform",
    }
)
_ALLOWED_CODES = frozenset(
    {
        "HARNESS_FAILED",
        "UNEXPECTED_ERROR",
        "TRANSFORM_NOT_FINITE",
        "TRANSFORM_SCALE_INVALID",
        "TRANSFORM_READ_FAILED",
        "ZOOM_IN_TIMEOUT",
        "BASELINE_RESTORE_TIMEOUT",
        "TRANSFORM_TIMEOUT",
        "BASELINE_READ_FAILED",
        "CONTROLLER_STOP_FAILED",
        "BASELINE_READBACK_MISMATCH",
        "BASELINE_RESTORE_FAILED",
        "INNER_HARNESS_CLEANUP_FAILED",
        "TRANSFORM_FAILED",
        "TRANSFORM_OPERATION_FAILED",
        "TRANSFORM_OBSERVATION_MISSING",
        "OBS_EXECUTABLE_REQUIRED",
        "OBS_PROFILE_COLLECTION_REQUIRED",
        PREP_LIVE_DISABLED,
        "ENDPOINT_HOST_FORBIDDEN",
        "ENDPOINT_PORT_FORBIDDEN",
        "ENDPOINT_PROBE_FAILED",
        "ENDPOINT_OCCUPIED",
        "ENDPOINT_TIMEOUT",
        "LISTENER_QUERY_UNKNOWN",
        "LISTENER_DATA_INVALID",
        "LISTENER_RECORD_INVALID",
        "LISTENER_FIELDS_MISSING",
        "LISTENER_FIELDS_INVALID",
        "LISTENER_PLATFORM_UNSUPPORTED",
        "LISTENER_QUERY_FAILED",
        "LISTENER_EXPOSED",
        "PROCESS_PID_INVALID",
        "LISTENER_OWNER_UNKNOWN",
        "LISTENER_OWNER_MISMATCH",
        "LIFECYCLE_OPERATION_FAILED",
        "OBS_DISCONNECT_FAILED",
        "OWNED_PROCESS_STOP_FAILED",
        "MUTEX_RELEASE_FAILED",
        "CLEANUP_FAILED",
        "RESULT_MISSING",
    }
)
_ALLOWED_CLEANUP_ERRORS = frozenset(
    {
        "cleanup:CONTROLLER_STOP_FAILED",
        "cleanup:BASELINE_READBACK_MISMATCH",
        "cleanup:BASELINE_RESTORE_FAILED",
        "cleanup:INNER_HARNESS_CLEANUP_FAILED",
        "cleanup:OBS_DISCONNECT_FAILED",
        "cleanup:OWNED_PROCESS_STOP_FAILED",
        "cleanup:MUTEX_RELEASE_FAILED",
        "cleanup:UNEXPECTED_CLEANUP_FAILURE",
    }
)
_SAFE_STAGE = "harness"
_SAFE_CODE = "UNEXPECTED_ERROR"
_SAFE_CLEANUP_ERROR = "cleanup:UNEXPECTED_CLEANUP_FAILURE"


class HarnessObs(Protocol):
    def get_transform(self, item: Any) -> Transform: ...

    def set_transform(self, item: Any, transform: Transform) -> None: ...


class LiveObs(Protocol):
    def connect(self) -> None: ...

    def get_canvas_size(self) -> tuple[float, float]: ...

    def find_display_capture(
        self,
        scene: str,
        source: str,
        *,
        displays: list[DisplayInfo],
        canvas: tuple[float, float],
    ) -> Any: ...

    def validate_transform_contract(self, item: Any, canvas: tuple[float, float]) -> None: ...


class OwnedProcess(Protocol):
    pid: int


@dataclass(frozen=True)
class PortableObsLaunch:
    argv: tuple[str, ...]
    cwd: Path


@dataclass(frozen=True)
class LifecyclePorts:
    """All side effects required by the future live path, explicitly injected."""

    mutex: Callable[[Path], ContextManager[Any]]
    endpoint_free: Callable[[str, int], None]
    spawn: Callable[[PortableObsLaunch], OwnedProcess]
    wait_endpoint: Callable[[str, int, float], None]
    listener_owned: Callable[[str, int, int], None]
    obs_factory: Callable[[str, int, str], LiveObs]
    pointer_factory: Callable[[], PointerPort]
    disconnect: Callable[[LiveObs], None]
    stop_process: Callable[[OwnedProcess], None]
    clock: Callable[[], float]
    sleep: Callable[[float], None]


class HarnessFailure(RuntimeError):
    """A safe, machine-readable harness failure.

    The preparation harness intentionally carries only an allow-listed stage and
    code.  Exception text, fixture values, child-process output, and cleanup
    exception text never become part of the public diagnostic.
    """

    def __init__(
        self,
        message: str = "harness failed",
        *,
        stage: str = "harness",
        code: str = "HARNESS_FAILED",
        cleanup_error: str | None = None,
        primary_error: str | None = None,
        transform: Transform | None = None,
    ) -> None:
        del message
        self.stage, self.code = _normalise_diagnostic(stage, code)
        self.cleanup_error = _normalise_cleanup_error(cleanup_error)
        self.primary_error = _normalise_primary_error(primary_error)
        self.transform = transform
        super().__init__(f"{self.stage}:{self.code}")

    def as_dict(self) -> dict[str, Any]:
        stage, code = _normalise_diagnostic(self.stage, self.code)
        return {
            "status": "blocked",
            "stage": stage,
            "code": code,
            "primary_error": _normalise_primary_error(self.primary_error),
            "cleanup_error": _normalise_cleanup_error(self.cleanup_error),
            "transform": _transform_dict(self.transform),
        }


@dataclass(frozen=True)
class HarnessResult:
    baseline: Transform
    zoomed: Transform
    restored: Transform
    cleanup_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "pass" if self.cleanup_error is None else "cleanup_failed",
            "baseline": _transform_dict(self.baseline),
            "zoomed": _transform_dict(self.zoomed),
            "restored": _transform_dict(self.restored),
            "cleanup_error": _normalise_cleanup_error(self.cleanup_error),
        }


def _finite_transform(transform: Transform) -> None:
    values = (transform.pos_x, transform.pos_y, transform.scale_x, transform.scale_y)
    if not all(math.isfinite(float(value)) for value in values):
        raise HarnessFailure(stage="transform", code="TRANSFORM_NOT_FINITE")
    if transform.scale_x <= 0 or transform.scale_y <= 0:
        raise HarnessFailure(stage="transform", code="TRANSFORM_SCALE_INVALID")


def _safe_error(
    error: BaseException,
    *,
    stage: str = "harness",
    code: str = "UNEXPECTED_ERROR",
    secrets: tuple[str, ...] = (),
) -> str:
    """Return only a fixed stage/code pair; ``secrets`` is compatibility-only."""

    del secrets
    if isinstance(error, HarnessFailure):
        stage, code = _normalise_diagnostic(error.stage, error.code)
    else:
        stage, code = _normalise_diagnostic(stage, code)
    return f"{stage}:{code}"


def _normalise_diagnostic(stage: object, code: object) -> tuple[str, str]:
    if (
        isinstance(stage, str)
        and stage in _ALLOWED_STAGES
        and isinstance(code, str)
        and code in _ALLOWED_CODES
    ):
        return stage, code
    return _SAFE_STAGE, _SAFE_CODE


def _normalise_primary_error(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stage, separator, code = value.partition(":")
        if separator and ":" not in code:
            safe_stage, safe_code = _normalise_diagnostic(stage, code)
            if (safe_stage, safe_code) == (stage, code):
                return value
    return f"{_SAFE_STAGE}:{_SAFE_CODE}"


def _normalise_cleanup_error(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return _SAFE_CLEANUP_ERROR
    if not value:
        return None
    errors = [part.strip() for part in value.split(";") if part.strip()]
    if not errors:
        return None
    return "; ".join(
        error if error in _ALLOWED_CLEANUP_ERRORS else _SAFE_CLEANUP_ERROR
        for error in errors
    )


def _transform_dict(value: Transform | None) -> dict[str, float] | None:
    if value is None:
        return None
    try:
        result = {
            "pos_x": float(value.pos_x),
            "pos_y": float(value.pos_y),
            "scale_x": float(value.scale_x),
            "scale_y": float(value.scale_y),
        }
    except Exception:
        return None
    if not all(math.isfinite(number) for number in result.values()):
        return None
    return result


def _close_enough(left: Transform, right: Transform, *, position: float, scale: float) -> bool:
    return (
        abs(left.pos_x - right.pos_x) <= position
        and abs(left.pos_y - right.pos_y) <= position
        and abs(left.scale_x - right.scale_x) <= scale
        and abs(left.scale_y - right.scale_y) <= scale
    )


def _is_zoomed(baseline: Transform, value: Transform, *, position: float, scale: float) -> bool:
    scale_changed = (
        value.scale_x > baseline.scale_x + scale
        and value.scale_y > baseline.scale_y + scale
    )
    position_changed = (
        abs(value.pos_x - baseline.pos_x) > position
        or abs(value.pos_y - baseline.pos_y) > position
    )
    return scale_changed and position_changed


def _poll_transform(
    obs: HarnessObs,
    item: Any,
    predicate: Callable[[Transform], bool],
    *,
    timeout: float,
    interval: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    description: str,
) -> Transform:
    deadline = clock() + timeout
    latest: Transform | None = None
    while clock() <= deadline:
        try:
            latest = obs.get_transform(item)
        except Exception:
            raise HarnessFailure(stage="transform", code="TRANSFORM_READ_FAILED") from None
        _finite_transform(latest)
        if predicate(latest):
            return latest
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(interval, remaining))
    timeout_code = {
        "zoom-in transform": "ZOOM_IN_TIMEOUT",
        "baseline restore": "BASELINE_RESTORE_TIMEOUT",
    }.get(description, "TRANSFORM_TIMEOUT")
    raise HarnessFailure(
        stage="transform",
        code=timeout_code,
        transform=latest,
    )


def run_transform_harness(
    *,
    obs: HarnessObs,
    item: Any,
    canvas: tuple[float, float],
    source: tuple[float, float],
    zoom_config: ZoomConfig,
    displays: list[DisplayInfo],
    pointer: PointerPort,
    capture_display: DisplayInfo | None = None,
    display_to_source_scale: tuple[float, float] = (1.0, 1.0),
    timeout: float = 10.0,
    poll_interval: float = 0.05,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    controller_factory: Callable[..., ZoomController] = ZoomController,
) -> HarnessResult:
    """Exercise the production controller and always restore the baseline.

    ``obs`` must be the same adapter boundary used by the application. Tests
    provide a fake implementing that boundary; real adapter construction is
    deliberately outside this offline preparation ticket.
    """

    if timeout <= 0 or poll_interval <= 0:
        raise ValueError("timeout and poll_interval must be positive")
    try:
        baseline = obs.get_transform(item)
        _finite_transform(baseline)
    except HarnessFailure:
        raise
    except Exception:
        raise HarnessFailure(stage="baseline", code="BASELINE_READ_FAILED") from None
    controller: ZoomController | None = None
    primary_error: BaseException | None = None
    cleanup_errors: list[str] = []
    zoomed: Transform | None = None
    restored: Transform | None = None
    cleanup_transform: Transform | None = None

    try:
        controller = controller_factory(
            obs=obs,
            item=item,
            orig=baseline,
            canvas=canvas,
            src=source,
            zoom_cfg=zoom_config,
            displays=displays,
            dry_run=False,
            pointer=pointer,
            clock=clock,
            wait=sleep,
            capture_display=capture_display,
            display_to_source_scale=display_to_source_scale,
        )
        controller.start()
        # This is the production command boundary. No live ASR is involved.
        controller.handle("zoom_in")
        zoomed = _poll_transform(
            obs,
            item,
            lambda value: _is_zoomed(
                baseline,
                value,
                position=0.5,
                scale=1e-4,
            ),
            timeout=timeout,
            interval=poll_interval,
            clock=clock,
            sleep=sleep,
            description="zoom-in transform",
        )
        controller.handle("zoom_out")
        restored = _poll_transform(
            obs,
            item,
            lambda value: _close_enough(
                value,
                baseline,
                position=0.5,
                scale=1e-4,
            ),
            timeout=timeout,
            interval=poll_interval,
            clock=clock,
            sleep=sleep,
            description="baseline restore",
        )
    except BaseException as exc:  # cleanup must run for assertion and runtime failures alike
        primary_error = exc
    finally:
        if controller is not None:
            try:
                controller.stop()
            except BaseException:
                cleanup_errors.append("cleanup:CONTROLLER_STOP_FAILED")
        try:
            # ZoomController.stop is ordered and best effort. The explicit
            # write/read-back makes cleanup failure observable to the gate,
            # including when controller construction itself failed.
            obs.set_transform(item, baseline)
            final_transform = obs.get_transform(item)
            _finite_transform(final_transform)
            cleanup_transform = final_transform
            if not _close_enough(
                final_transform,
                baseline,
                position=0.5,
                scale=1e-4,
            ):
                cleanup_errors.append("cleanup:BASELINE_READBACK_MISMATCH")
        except BaseException:
            cleanup_errors.append("cleanup:BASELINE_RESTORE_FAILED")

    cleanup_error = "; ".join(cleanup_errors) or None
    if primary_error is not None:
        if isinstance(primary_error, HarnessFailure) and primary_error.cleanup_error:
            cleanup_errors.insert(0, "cleanup:INNER_HARNESS_CLEANUP_FAILED")
        cleanup_error = "; ".join(cleanup_errors) or None
        raise HarnessFailure(
            stage="transform",
            code="TRANSFORM_FAILED",
            cleanup_error=cleanup_error,
            primary_error=_safe_error(
                primary_error,
                stage="transform",
                code="TRANSFORM_OPERATION_FAILED",
            ),
            transform=cleanup_transform or zoomed,
        ) from None
    if cleanup_error:
        raise HarnessFailure(
            stage="cleanup",
            code="CLEANUP_FAILED",
            cleanup_error=cleanup_error,
            transform=cleanup_transform,
        )
    if zoomed is None or restored is None:
        raise HarnessFailure(stage="transform", code="TRANSFORM_OBSERVATION_MISSING")
    return HarnessResult(baseline, zoomed, restored)


def build_portable_obs_launch(
    executable: str | Path, profile: str, collection: str
) -> PortableObsLaunch:
    """Build the future OBS command without touching the filesystem or process table."""

    executable_text = str(executable).strip()
    if not executable_text:
        raise HarnessFailure(stage="portable", code="OBS_EXECUTABLE_REQUIRED")
    if not profile.strip() or not collection.strip():
        raise HarnessFailure(stage="portable", code="OBS_PROFILE_COLLECTION_REQUIRED")
    executable_path = Path(executable_text)
    return PortableObsLaunch(
        argv=(
            executable_text,
            "--portable",
            "--profile",
            profile,
            "--collection",
            collection,
        ),
        cwd=executable_path.parent,
    )


def _raise_prep_live_disabled() -> None:
    raise HarnessFailure(stage="preparation", code=PREP_LIVE_DISABLED)


def assert_endpoint_free(
    host: str,
    port: int,
    *,
    timeout: float = 1.0,
    connect: Callable[..., Any] = socket.create_connection,
    listener_inspector: Callable[[int], list[TcpListener]] = inspect_tcp_listeners,
) -> None:
    """Prove the localhost OBS port is unused before starting owned OBS."""

    try:
        lab_assert_endpoint_free(
            host,
            port,
            timeout=timeout,
            connect=connect,
            inspector=listener_inspector,
        )
    except LabSafetyError as exc:
        raise HarnessFailure(
            stage="endpoint",
            code=exc.code,
            primary_error=_safe_error(exc, stage="endpoint", code=exc.code),
        ) from None


def assert_listener_owned(
    host: str,
    port: int,
    owning_pid: int,
    *,
    listener_inspector: Callable[[int], list[TcpListener]] = inspect_tcp_listeners,
) -> None:
    """Prove the ready listener is loopback-only and owned by our OBS PID."""

    try:
        lab_assert_listener_owned(
            host,
            port,
            owning_pid,
            inspector=listener_inspector,
        )
    except LabSafetyError as exc:
        raise HarnessFailure(
            stage="listener",
            code=exc.code,
            primary_error=_safe_error(exc, stage="listener", code=exc.code),
        ) from None


def wait_for_endpoint(
    host: str,
    port: int,
    *,
    timeout: float = 30.0,
    interval: float = 0.25,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wait for the owned OBS WebSocket TCP endpoint without logging secrets."""

    if host != DEFAULT_HOST:
        raise HarnessFailure(stage="endpoint", code="ENDPOINT_HOST_FORBIDDEN")
    if port != DEFAULT_PORT:
        raise HarnessFailure(stage="endpoint", code="ENDPOINT_PORT_FORBIDDEN")
    deadline = clock() + timeout
    while clock() <= deadline:
        try:
            with socket.create_connection((host, port), timeout=min(interval, 1.0)):
                return
        except OSError:
            sleep(min(interval, max(0.0, deadline - clock())))
    raise HarnessFailure(stage="endpoint", code="ENDPOINT_TIMEOUT")


def _start_obs(
    executable: str,
    profile: str,
    collection: str,
    *,
    lab_root: Path | None = None,
    portable: bool = False,
) -> OwnedProcess:
    del executable, profile, collection, lab_root, portable
    _raise_prep_live_disabled()


def _stop_owned_process(process: OwnedProcess) -> str | None:
    del process
    _raise_prep_live_disabled()


def _close_obs(obs: Any) -> str | None:
    del obs
    _raise_prep_live_disabled()


def run_live_harness(*args: Any, **kwargs: Any) -> HarnessResult:
    del args, kwargs
    _raise_prep_live_disabled()


def run_injected_lifecycle(
    *,
    lab_root: Path,
    executable: str | Path,
    profile: str,
    collection: str,
    host: str,
    port: int,
    password: str,
    scene: str,
    source: str,
    zoom_config: ZoomConfig,
    timeout: float,
    ports: LifecyclePorts,
    controller_factory: Callable[..., ZoomController],
) -> HarnessResult:
    """Exercise the lifecycle only with explicitly injected fake/owned ports.

    This is an offline contract seam.  It has no real OBS, pointer, process,
    socket, mutex, or client defaults.  The future W11-011 activation may wire
    real implementations in a protected change; PREP deliberately cannot.
    """

    launch = build_portable_obs_launch(executable, profile, collection)
    process: OwnedProcess | None = None
    obs: LiveObs | None = None
    result: HarnessResult | None = None
    primary_error: BaseException | None = None
    cleanup_errors: list[str] = []
    mutex_entered = False

    try:
        with ports.mutex(lab_root):
            mutex_entered = True
            try:
                ports.endpoint_free(host, port)
                process = ports.spawn(launch)
                process_pid = getattr(process, "pid", 0)
                if not isinstance(process_pid, int) or process_pid <= 0:
                    raise HarnessFailure(stage="spawn", code="PROCESS_PID_INVALID")
                ports.wait_endpoint(host, port, timeout)
                ports.listener_owned(host, port, process_pid)
                obs = ports.obs_factory(host, port, password)
                obs.connect()
                pointer = ports.pointer_factory()
                displays = pointer.get_displays()
                canvas = obs.get_canvas_size()
                item = obs.find_display_capture(
                    scene,
                    source,
                    displays=displays,
                    canvas=canvas,
                )
                obs.validate_transform_contract(item, canvas)
                result = run_transform_harness(
                    obs=obs,
                    item=item,
                    canvas=canvas,
                    source=(item.source_width, item.source_height),
                    zoom_config=zoom_config,
                    displays=displays,
                    pointer=pointer,
                    capture_display=item.capture_display,
                    display_to_source_scale=item.display_to_source_scale,
                    timeout=timeout,
                    clock=ports.clock,
                    sleep=ports.sleep,
                    controller_factory=controller_factory,
                )
            except BaseException as exc:
                primary_error = exc
            finally:
                if obs is not None:
                    try:
                        ports.disconnect(obs)
                    except BaseException:
                        cleanup_errors.append("cleanup:OBS_DISCONNECT_FAILED")
                if process is not None:
                    try:
                        ports.stop_process(process)
                    except BaseException:
                        cleanup_errors.append("cleanup:OWNED_PROCESS_STOP_FAILED")
    except BaseException as exc:
        if mutex_entered:
            cleanup_errors.append("cleanup:MUTEX_RELEASE_FAILED")
        elif primary_error is None:
            primary_error = exc

    if primary_error is not None:
        if isinstance(primary_error, HarnessFailure) and primary_error.cleanup_error:
            cleanup_errors.insert(0, "cleanup:INNER_HARNESS_CLEANUP_FAILED")
        raise HarnessFailure(
            stage="lifecycle",
            code=(
                primary_error.code
                if isinstance(primary_error, HarnessFailure)
                else "LIFECYCLE_OPERATION_FAILED"
            ),
            primary_error=_safe_error(
                primary_error,
                stage="lifecycle",
                code="LIFECYCLE_OPERATION_FAILED",
            ),
            cleanup_error="; ".join(cleanup_errors) or None,
            transform=(
                primary_error.transform if isinstance(primary_error, HarnessFailure) else None
            ),
        ) from None
    if cleanup_errors:
        raise HarnessFailure(
            stage="cleanup",
            code="CLEANUP_FAILED",
            cleanup_error="; ".join(cleanup_errors),
        )
    if result is None:
        raise HarnessFailure(stage="lifecycle", code="RESULT_MISSING")
    return result


def _run_live_harness_locked(**ignored: Any) -> HarnessResult:
    """Compatibility name retained without a real or default implementation."""

    del ignored
    _raise_prep_live_disabled()


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_known_args()
    print(
        json.dumps(
            {
                "status": "blocked",
                "stage": "preparation",
                "code": PREP_LIVE_DISABLED,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
