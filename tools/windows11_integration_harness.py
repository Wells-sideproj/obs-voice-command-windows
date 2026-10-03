"""Deterministic command-to-transform harness for the deferred Layer B path.

The workflow that calls this module is intentionally inactive until W11-011.
The required command source is a direct ``ZoomController.handle`` call rather
than live speech recognition, so the same production controller and
``ObsClient`` boundaries can be tested without a microphone or ASR model.
"""

from __future__ import annotations

import argparse
import errno
import json
import math
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from obs_voice_command.config import ZoomConfig, load_config
from obs_voice_command.obs_client import ObsClient
from obs_voice_command.platform.common import DisplayInfo
from obs_voice_command.runtime import PointerPort, SystemPointer, ZoomController
from obs_voice_command.zoom import Transform


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4455
DEFAULT_AUTHORIZATION_ENV = "W11_010_HARDWARE_AUTHORIZED"
EXPECTED_AUTHORIZATION = "approved"


class HarnessObs(Protocol):
    def get_transform(self, item: Any) -> Transform: ...

    def set_transform(self, item: Any, transform: Transform) -> None: ...


class HarnessFailure(RuntimeError):
    """A primary harness failure or an independently reported cleanup failure."""

    def __init__(self, message: str, *, cleanup_error: str | None = None) -> None:
        super().__init__(message)
        self.cleanup_error = cleanup_error


@dataclass(frozen=True)
class HarnessResult:
    baseline: Transform
    zoomed: Transform
    restored: Transform
    cleanup_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        def serialise(value: Transform) -> dict[str, float]:
            return {
                "pos_x": value.pos_x,
                "pos_y": value.pos_y,
                "scale_x": value.scale_x,
                "scale_y": value.scale_y,
            }

        return {
            "status": "pass" if self.cleanup_error is None else "cleanup_failed",
            "baseline": serialise(self.baseline),
            "zoomed": serialise(self.zoomed),
            "restored": serialise(self.restored),
            "cleanup_error": self.cleanup_error,
        }


def _finite_transform(transform: Transform) -> None:
    values = (transform.pos_x, transform.pos_y, transform.scale_x, transform.scale_y)
    if not all(math.isfinite(float(value)) for value in values):
        raise HarnessFailure("OBS returned a non-finite transform")
    if transform.scale_x <= 0 or transform.scale_y <= 0:
        raise HarnessFailure("OBS returned a non-positive transform scale")


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
        latest = obs.get_transform(item)
        _finite_transform(latest)
        if predicate(latest):
            return latest
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(interval, remaining))
    raise HarnessFailure(
        f"timed out waiting for {description}; last_transform={latest!r}"
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
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    controller_factory: Callable[..., ZoomController] = ZoomController,
) -> HarnessResult:
    """Exercise the production controller and always restore the baseline.

    ``obs`` must be the same adapter boundary used by the application. Tests
    provide a fake implementing that boundary; the live CLI supplies an
    :class:`~obs_voice_command.obs_client.ObsClient`.
    """

    if timeout <= 0 or poll_interval <= 0:
        raise ValueError("timeout and poll_interval must be positive")
    baseline = obs.get_transform(item)
    _finite_transform(baseline)
    controller: ZoomController | None = None
    primary_error: BaseException | None = None
    cleanup_errors: list[str] = []
    zoomed: Transform | None = None
    restored: Transform | None = None

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
            except BaseException as exc:
                cleanup_errors.append(f"controller stop failed: {type(exc).__name__}: {exc}")
            try:
                # ZoomController.stop is ordered and best effort. The explicit
                # write/read-back makes cleanup failure observable to the gate.
                obs.set_transform(item, baseline)
                final_transform = obs.get_transform(item)
                _finite_transform(final_transform)
                if not _close_enough(
                    final_transform,
                    baseline,
                    position=0.5,
                    scale=1e-4,
                ):
                    cleanup_errors.append(
                        f"baseline read-back mismatch: {final_transform!r} != {baseline!r}"
                    )
            except BaseException as exc:
                cleanup_errors.append(
                    f"baseline cleanup failed: {type(exc).__name__}: {exc}"
                )

    cleanup_error = "; ".join(cleanup_errors) or None
    if primary_error is not None:
        inner_cleanup_error = getattr(primary_error, "cleanup_error", None)
        if inner_cleanup_error:
            cleanup_errors.insert(0, f"inner harness cleanup_error: {inner_cleanup_error}")
        cleanup_error = "; ".join(cleanup_errors) or None
        message = f"harness failed: {type(primary_error).__name__}: {primary_error}"
        if cleanup_error:
            message += f"; cleanup_error={cleanup_error}"
        raise HarnessFailure(message, cleanup_error=cleanup_error) from primary_error
    if cleanup_error:
        raise HarnessFailure(
            "harness completed but cleanup failed",
            cleanup_error=cleanup_error,
        )
    if zoomed is None or restored is None:
        raise HarnessFailure("harness completed without both transform observations")
    return HarnessResult(baseline, zoomed, restored)


def _require_live_authorization(environment: MappingLike, variable: str) -> None:
    value = environment.get(variable, "")
    if value != EXPECTED_AUTHORIZATION:
        raise HarnessFailure(
            f"{variable} must contain the separately recorded hardware authorization"
        )


class MappingLike(Protocol):
    def get(self, key: str, default: str = "") -> str: ...


def _is_connection_refused(error: OSError) -> bool:
    return (
        isinstance(error, ConnectionRefusedError)
        or error.errno in {errno.ECONNREFUSED, 61, 10061}
        or getattr(error, "winerror", None) == 10061
    )


def assert_endpoint_free(
    host: str,
    port: int,
    *,
    timeout: float = 1.0,
    connect: Callable[..., Any] = socket.create_connection,
) -> None:
    """Prove the localhost OBS port is unused before starting owned OBS."""

    if host != DEFAULT_HOST:
        raise HarnessFailure("Layer B OBS endpoint is hard-locked to 127.0.0.1")
    try:
        connection = connect((host, port), timeout=timeout)
    except OSError as exc:
        if _is_connection_refused(exc):
            return
        raise HarnessFailure(
            f"unable to prove OBS endpoint {host}:{port} is free: {type(exc).__name__}"
        ) from exc
    try:
        connection.close()
    finally:
        raise HarnessFailure(f"OBS endpoint {host}:{port} is already occupied")


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

    deadline = clock() + timeout
    last_error: str | None = None
    while clock() <= deadline:
        try:
            with socket.create_connection((host, port), timeout=min(interval, 1.0)):
                return
        except OSError as exc:
            last_error = type(exc).__name__
            sleep(min(interval, max(0.0, deadline - clock())))
    raise HarnessFailure(
        f"OBS endpoint {host}:{port} did not become reachable; last_error={last_error}"
    )


def _start_obs(executable: str, profile: str, collection: str) -> subprocess.Popen[Any]:
    if not executable:
        raise HarnessFailure("OBS executable path is required")
    command = [executable, "--profile", profile, "--collection", collection]
    creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
    except OSError as exc:
        raise HarnessFailure(f"unable to start dedicated OBS: {type(exc).__name__}") from exc


def _stop_owned_process(process: subprocess.Popen[Any]) -> str | None:
    if process.poll() is not None:
        return None
    try:
        process.terminate()
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired) as kill_exc:
            return (
                "owned OBS cleanup failed: "
                f"{type(exc).__name__}; kill={type(kill_exc).__name__}"
            )
    return None


def _close_obs(obs: Any) -> str | None:
    close = getattr(obs, "close", None)
    if callable(close):
        try:
            close()
            return None
        except Exception as exc:  # report cleanup without exposing connection details
            return f"OBS close failed: {type(exc).__name__}"
    client = getattr(obs, "_client", None)
    disconnect = getattr(client, "disconnect", None)
    if callable(disconnect):
        try:
            disconnect()
            return None
        except Exception as exc:
            return f"OBS disconnect failed: {type(exc).__name__}"
    return None


def run_live_harness(
    *,
    executable: str,
    profile: str,
    collection: str,
    host: str,
    port: int,
    password: str,
    scene: str,
    source: str,
    zoom_config: ZoomConfig,
    timeout: float,
) -> HarnessResult:
    """Run the explicitly authorized real-OBS path used by future W11-011."""

    if host != DEFAULT_HOST:
        raise HarnessFailure("Layer B OBS endpoint is hard-locked to 127.0.0.1")
    assert_endpoint_free(host, port)
    process = _start_obs(executable, profile, collection)
    obs = ObsClient(host, port, password)
    primary_error: BaseException | None = None
    result: HarnessResult | None = None
    cleanup_errors: list[str] = []
    try:
        wait_for_endpoint(host, port, timeout=timeout)
        if process.poll() is not None:
            raise HarnessFailure("dedicated OBS exited before its endpoint became ready")
        obs.connect()
        pointer = SystemPointer()
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
        )
    except BaseException as exc:
        primary_error = exc
    finally:
        close_error = _close_obs(obs)
        if close_error:
            cleanup_errors.append(close_error)
        process_error = _stop_owned_process(process)
        if process_error:
            cleanup_errors.append(process_error)

    cleanup_error = "; ".join(cleanup_errors) or None
    if primary_error is not None:
        inner_cleanup_error = getattr(primary_error, "cleanup_error", None)
        if inner_cleanup_error:
            cleanup_errors.insert(0, f"inner harness cleanup_error: {inner_cleanup_error}")
        cleanup_error = "; ".join(cleanup_errors) or None
        message = f"live harness failed: {type(primary_error).__name__}"
        if cleanup_error:
            message += f"; cleanup_error={cleanup_error}"
        raise HarnessFailure(message, cleanup_error=cleanup_error) from primary_error
    if cleanup_error:
        raise HarnessFailure("live harness cleanup failed", cleanup_error=cleanup_error)
    if result is None:
        raise HarnessFailure("live harness completed without a result")
    return result


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--obs-executable", required=True)
    parser.add_argument("--profile", default="W11-010")
    parser.add_argument("--collection", default="W11-010")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--scene", default="")
    parser.add_argument("--source", default="")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--password-env", default="OBS_WEBSOCKET_PASSWORD")
    parser.add_argument("--authorization-env", default=DEFAULT_AUTHORIZATION_ENV)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    try:
        _require_live_authorization(os.environ, args.authorization_env)
        if os.environ.get("GITHUB_ACTIONS") != "true":
            raise HarnessFailure("real Layer B harness must run from GitHub Actions")
        config = load_config(Path(args.config))
        scene = args.scene or config.obs.scene
        source = args.source or config.obs.source
        password = os.environ.get(args.password_env, "")
        if not password:
            raise HarnessFailure("OBS WebSocket password environment variable is empty")
        result = run_live_harness(
            executable=args.obs_executable,
            profile=args.profile,
            collection=args.collection,
            host=DEFAULT_HOST,
            port=args.port,
            password=password,
            scene=scene,
            source=source,
            zoom_config=config.zoom,
            timeout=args.timeout,
        )
        print(json.dumps(result.as_dict(), sort_keys=True))
        return 0
    except HarnessFailure as exc:
        payload = {"status": "blocked", "error": str(exc)}
        if exc.cleanup_error:
            payload["cleanup_error"] = exc.cleanup_error
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
