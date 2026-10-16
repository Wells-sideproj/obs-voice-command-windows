from __future__ import annotations

import contextlib
import sys
import time
from pathlib import Path

import pytest

from tools import windows11_integration_harness as harness_module
from obs_voice_command.config import ZoomConfig
from obs_voice_command.platform.common import DisplayInfo
from obs_voice_command.zoom import Transform
from tools.windows11_integration_harness import (
    HarnessFailure,
    LifecyclePorts,
    PREP_LIVE_DISABLED,
    assert_endpoint_free,
    build_portable_obs_launch,
    run_injected_lifecycle,
    run_live_harness,
    run_transform_harness,
)


DISPLAY = DisplayInfo(
    origin_x=0,
    origin_y=0,
    width_pts=1920,
    height_pts=1080,
    width_px=1920,
    height_px=1080,
    id="fake-display",
)
BASELINE = Transform(pos_x=7.0, pos_y=11.0, scale_x=1.0, scale_y=1.0)


class FakePointer:
    def get_displays(self):
        return [DISPLAY]

    def get_cursor_position(self):
        return (960.0, 540.0)

    def locate(self, position, displays):
        del position
        return displays[0], 960.0, 540.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.calls = 0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        self.calls += 1
        return self.now

    def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)
        self.now += max(0.0, duration)
        # Yield to the controller thread while keeping elapsed time synthetic.
        time.sleep(0.001)


class FakeObs:
    def __init__(self, *, fail_cleanup: bool = False):
        self.transform = BASELINE
        self.transforms: list[Transform] = []
        self.fail_cleanup = fail_cleanup

    def get_transform(self, item):
        del item
        return self.transform

    def set_transform(self, item, transform):
        del item
        if self.fail_cleanup and transform == BASELINE:
            raise RuntimeError("simulated baseline write failure")
        self.transform = transform
        self.transforms.append(transform)


def _run(obs: FakeObs):
    clock = FakeClock()
    return run_transform_harness(
        obs=obs,
        item=object(),
        canvas=(1920.0, 1080.0),
        source=(1920.0, 1080.0),
        zoom_config=ZoomConfig(level=2.0, deadzone=0.0, smoothing=1.0),
        displays=[DISPLAY],
        pointer=FakePointer(),
        timeout=1.0,
        poll_interval=0.001,
        clock=clock,
        sleep=clock.sleep,
    )


def test_harness_uses_production_controller_and_restores_baseline() -> None:
    obs = FakeObs()

    result = _run(obs)

    assert result.baseline == BASELINE
    assert result.zoomed.scale_x == pytest.approx(2.0)
    assert result.zoomed.scale_y == pytest.approx(2.0)
    assert result.restored == BASELINE
    assert obs.transform == BASELINE
    assert result.cleanup_error is None


def test_harness_reports_cleanup_failure_separately() -> None:
    with pytest.raises(HarnessFailure) as raised:
        _run(FakeObs(fail_cleanup=True))

    assert raised.value.cleanup_error is not None
    assert "cleanup:BASELINE_RESTORE_FAILED" in raised.value.cleanup_error


def test_harness_failure_allowlists_diagnostics_and_finite_transform_values() -> None:
    failure = HarnessFailure(
        "password=sentinel\nTOKEN",
        stage="secret-stage\nTOKEN",
        code="secret-code",
        primary_error="primary secret\nTOKEN",
        cleanup_error="cleanup:secret-cleanup\nnested",
        transform=Transform(float("nan"), 1.0, float("inf"), 1.0),
    )

    payload = failure.as_dict()

    assert str(failure) == "harness:UNEXPECTED_ERROR"
    assert payload == {
        "status": "blocked",
        "stage": "harness",
        "code": "UNEXPECTED_ERROR",
        "primary_error": "harness:UNEXPECTED_ERROR",
        "cleanup_error": "cleanup:UNEXPECTED_CLEANUP_FAILURE",
        "transform": None,
    }
    assert "sentinel" not in str(failure)
    assert "TOKEN" not in str(payload)


def test_harness_failure_preserves_only_known_diagnostic_codes() -> None:
    failure = HarnessFailure(
        stage="transform",
        code="TRANSFORM_FAILED",
        primary_error="transform:TRANSFORM_OPERATION_FAILED",
        cleanup_error="cleanup:BASELINE_RESTORE_FAILED",
        transform=BASELINE,
    )

    assert failure.as_dict() == {
        "status": "blocked",
        "stage": "transform",
        "code": "TRANSFORM_FAILED",
        "primary_error": "transform:TRANSFORM_OPERATION_FAILED",
        "cleanup_error": "cleanup:BASELINE_RESTORE_FAILED",
        "transform": {
            "pos_x": 7.0,
            "pos_y": 11.0,
            "scale_x": 1.0,
            "scale_y": 1.0,
        },
    }


def test_nested_harness_cleanup_error_is_preserved() -> None:
    clock = FakeClock()

    def fail_inside_controller(**kwargs):
        del kwargs
        raise HarnessFailure(
            stage="inner",
            code="INNER_TRANSFORM_FAILED",
            cleanup_error="cleanup:INNER_BASELINE_FAILED",
        )

    with pytest.raises(HarnessFailure) as raised:
        run_transform_harness(
            obs=FakeObs(),
            item=object(),
            canvas=(1920.0, 1080.0),
            source=(1920.0, 1080.0),
            zoom_config=ZoomConfig(level=2.0, deadzone=0.0, smoothing=1.0),
            displays=[DISPLAY],
            pointer=FakePointer(),
            timeout=1.0,
            poll_interval=0.001,
            clock=clock,
            sleep=clock.sleep,
            controller_factory=fail_inside_controller,
        )

    assert raised.value.cleanup_error == "cleanup:INNER_HARNESS_CLEANUP_FAILED"
    assert "inner baseline cleanup failed" not in str(raised.value)


class FakeLiveObs(FakeObs):
    def connect(self):
        return None

    def get_canvas_size(self):
        return (1920.0, 1080.0)

    def find_display_capture(self, scene, source, *, displays, canvas):
        del scene, source, displays, canvas
        return type(
            "Capture",
            (),
            {
                "source_width": 1920.0,
                "source_height": 1080.0,
                "capture_display": DISPLAY,
                "display_to_source_scale": (1.0, 1.0),
            },
        )()

    def validate_transform_contract(self, item, canvas):
        del item, canvas


class FakeProcess:
    pid = 1234


def _lifecycle_ports(
    events: list[str], *, obs_factory=None, disconnect=None, stop=None, clock=None, mutex=None
):
    clock = clock or FakeClock()

    @contextlib.contextmanager
    def default_mutex(path: Path):
        events.append(f"mutex-enter:{path.name}")
        try:
            yield
        finally:
            events.append("mutex-exit")

    return LifecyclePorts(
        mutex=mutex or default_mutex,
        endpoint_free=lambda host, port: events.append(f"free:{host}:{port}"),
        spawn=lambda launch: events.append(f"spawn:{launch.cwd.name}") or FakeProcess(),
        wait_endpoint=lambda host, port, timeout: events.append("wait"),
        listener_owned=lambda host, port, pid: events.append(f"owned:{pid}"),
        obs_factory=obs_factory or (lambda host, port, password: FakeLiveObs()),
        pointer_factory=lambda: FakePointer(),
        disconnect=disconnect or (lambda obs: events.append("disconnect")),
        stop_process=stop or (lambda process: events.append(f"stop:{process.pid}")),
        clock=clock,
        sleep=clock.sleep,
    )


def _run_injected(
    events: list[str],
    *,
    ports: LifecyclePorts | None = None,
    controller_factory=harness_module.ZoomController,
    clock: FakeClock | None = None,
):
    executable = Path("C:/w11-obs-lab/obs/bin/64bit/obs64.exe")
    clock = clock or FakeClock()
    return run_injected_lifecycle(
        lab_root=Path("C:/w11-obs-lab"),
        executable=executable,
        profile="w11-011-certification",
        collection="w11-011-certification",
        host="127.0.0.1",
        port=4455,
        password="secret-sentinel",
        scene="Test Scene",
        source="Display Capture",
        zoom_config=ZoomConfig(level=2.0, deadzone=0.0, smoothing=1.0),
        timeout=1.0,
        ports=ports or _lifecycle_ports(events, clock=clock),
        controller_factory=controller_factory,
    )


def test_portable_launch_uses_executable_parent_as_cwd() -> None:
    launch = build_portable_obs_launch(
        Path("C:/w11-obs-lab/obs/bin/64bit/obs64.exe"),
        "w11-011-certification",
        "w11-011-certification",
    )

    assert launch.argv == (
        str(Path("C:/w11-obs-lab/obs/bin/64bit/obs64.exe")),
        "--portable",
        "--profile",
        "w11-011-certification",
        "--collection",
        "w11-011-certification",
    )
    assert launch.cwd == Path("C:/w11-obs-lab/obs/bin/64bit")


def test_public_live_wrapper_is_unconditionally_disabled(monkeypatch) -> None:
    monkeypatch.setattr(harness_module, "_start_obs", lambda *args, **kwargs: pytest.fail("spawned"))

    with pytest.raises(HarnessFailure) as raised:
        run_live_harness(
            executable="C:/secret/obs64.exe",
            profile="profile",
            collection="collection",
            host="127.0.0.1",
            port=4455,
            password="password-sentinel",
            scene="scene",
            source="source",
            zoom_config=ZoomConfig(level=2.0, deadzone=0.0, smoothing=1.0),
            timeout=1.0,
            approved="approved",
        )

    assert raised.value.code == PREP_LIVE_DISABLED
    assert "password-sentinel" not in str(raised.value)


def test_harness_cli_reports_fixed_disabled_code_before_reading_inputs(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "windows11_integration_harness.py",
            "--lab-fixture",
            "C:/secret/sentinel-fixture.toml",
            "--password-env",
            "SENTINEL_PASSWORD",
            "--authorization-env",
            "SENTINEL_AUTHORIZATION",
        ],
    )

    assert harness_module._cli() == 1
    error_output = capsys.readouterr().err
    assert PREP_LIVE_DISABLED in error_output
    assert "sentinel-fixture" not in error_output
    assert "SENTINEL_PASSWORD" not in error_output
    assert capsys.readouterr().out == ""


def test_injected_lifecycle_cleans_up_inside_mutex() -> None:
    events: list[str] = []

    result = _run_injected(events)

    assert result.restored == BASELINE
    assert events == [
        "mutex-enter:w11-obs-lab",
        "free:127.0.0.1:4455",
        "spawn:64bit",
        "wait",
        "owned:1234",
        "disconnect",
        "stop:1234",
        "mutex-exit",
    ]


def test_injected_lifecycle_passes_fake_clock_and_sleep() -> None:
    events: list[str] = []
    clock = FakeClock()

    result = _run_injected(events, clock=clock)

    assert result.restored == BASELINE
    assert clock.calls > 0


def test_injected_lifecycle_preserves_mutex_release_failure_with_primary_failure() -> None:
    events: list[str] = []

    @contextlib.contextmanager
    def release_fails(path: Path):
        events.append(f"mutex-enter:{path.name}")
        try:
            yield
        finally:
            events.append("mutex-exit")
            raise RuntimeError("mutex-release-secret-sentinel\nTOKEN")

    class FailingController:
        def __init__(self, **kwargs):
            del kwargs

        def start(self):
            raise RuntimeError("primary-secret-sentinel\nTOKEN")

        def stop(self):
            events.append("controller-stop")

    ports = _lifecycle_ports(events, mutex=release_fails)
    with pytest.raises(HarnessFailure) as raised:
        _run_injected(events, ports=ports, controller_factory=FailingController)

    assert raised.value.code == "TRANSFORM_FAILED"
    assert raised.value.cleanup_error == "cleanup:MUTEX_RELEASE_FAILED"
    assert "secret-sentinel" not in str(raised.value)
    assert events[-4:] == ["controller-stop", "disconnect", "stop:1234", "mutex-exit"]


def test_injected_lifecycle_preserves_mutex_release_failure_after_success() -> None:
    events: list[str] = []

    @contextlib.contextmanager
    def release_fails(path: Path):
        events.append(f"mutex-enter:{path.name}")
        try:
            yield
        finally:
            events.append("mutex-exit")
            raise RuntimeError("release-secret-sentinel")

    ports = _lifecycle_ports(events, mutex=release_fails)
    with pytest.raises(HarnessFailure) as raised:
        _run_injected(events, ports=ports)

    assert raised.value.code == "CLEANUP_FAILED"
    assert raised.value.primary_error is None
    assert raised.value.cleanup_error == "cleanup:MUTEX_RELEASE_FAILED"
    assert "release-secret-sentinel" not in str(raised.value)


def test_injected_lifecycle_acquire_failure_does_not_cleanup_unowned_process() -> None:
    events: list[str] = []

    class AcquireFails:
        def __enter__(self):
            raise RuntimeError("acquire-secret-sentinel")

        def __exit__(self, exc_type, exc, traceback):
            del exc_type, exc, traceback
            events.append("mutex-exit")

    def acquire_fails(path: Path):
        del path
        return AcquireFails()

    ports = _lifecycle_ports(events, mutex=acquire_fails)
    with pytest.raises(HarnessFailure) as raised:
        _run_injected(events, ports=ports)

    assert raised.value.code == "LIFECYCLE_OPERATION_FAILED"
    assert raised.value.cleanup_error is None
    assert events == []


def test_injected_lifecycle_stops_owned_process_when_obs_constructor_fails() -> None:
    events: list[str] = []

    def fail_constructor(host, port, password):
        del host, port, password
        raise RuntimeError("constructor-secret-sentinel")

    ports = _lifecycle_ports(events, obs_factory=fail_constructor)
    with pytest.raises(HarnessFailure) as raised:
        _run_injected(events, ports=ports)

    assert raised.value.primary_error == "lifecycle:LIFECYCLE_OPERATION_FAILED"
    assert raised.value.cleanup_error is None
    assert "constructor-secret-sentinel" not in str(raised.value)
    assert events[-3:] == ["owned:1234", "stop:1234", "mutex-exit"]


def test_injected_lifecycle_reports_transform_and_cleanup_codes_without_raw_text() -> None:
    events: list[str] = []

    class FailingController:
        def __init__(self, **kwargs):
            del kwargs

        def start(self):
            raise HarnessFailure(
                stage="transform",
                code="TRANSFORM_SENTINEL_FAILURE",
                cleanup_error="cleanup:INNER_SENTINEL_FAILURE",
            )

        def stop(self):
            events.append("controller-stop")

    def fail_disconnect(obs):
        del obs
        raise RuntimeError("disconnect-secret-sentinel")

    def fail_stop(process):
        del process
        raise RuntimeError("stop-secret-sentinel")

    ports = _lifecycle_ports(events, disconnect=fail_disconnect, stop=fail_stop)
    with pytest.raises(HarnessFailure) as raised:
        _run_injected(events, ports=ports, controller_factory=FailingController)

    assert raised.value.code == "TRANSFORM_FAILED"
    assert raised.value.cleanup_error == (
        "cleanup:INNER_HARNESS_CLEANUP_FAILED; "
        "cleanup:OBS_DISCONNECT_FAILED; cleanup:OWNED_PROCESS_STOP_FAILED"
    )
    assert "secret-sentinel" not in str(raised.value)
    assert events == [
        "mutex-enter:w11-obs-lab",
        "free:127.0.0.1:4455",
        "spawn:64bit",
        "wait",
        "owned:1234",
        "controller-stop",
        "mutex-exit",
    ]


class OccupiedConnection:
    def close(self):
        return None


def test_live_preflight_fails_closed_when_obs_port_is_occupied() -> None:
    with pytest.raises(HarnessFailure) as raised:
        assert_endpoint_free(
            "127.0.0.1",
            4455,
            connect=lambda address, timeout: OccupiedConnection(),
        )
    assert raised.value.code == "ENDPOINT_OCCUPIED"


def test_live_preflight_rejects_wildcard_listener_even_when_connect_is_refused() -> None:
    from tools.windows11_lab import LabSafetyError, TcpListener, assert_endpoint_free as lab_assert_endpoint_free

    with pytest.raises(LabSafetyError, match="wildcard"):
        lab_assert_endpoint_free(
            "127.0.0.1",
            4455,
            connect=lambda address, timeout: (_ for _ in ()).throw(ConnectionRefusedError()),
            inspector=lambda port: [TcpListener("0.0.0.0", port, 99)],
        )


def test_live_preflight_rejects_listener_owned_by_another_process() -> None:
    from tools.windows11_lab import LabSafetyError, TcpListener, assert_listener_owned

    with pytest.raises(LabSafetyError, match="not owned"):
        assert_listener_owned(
            "127.0.0.1",
            4455,
            1234,
            inspector=lambda port: [TcpListener("127.0.0.1", port, 5678)],
        )


def test_live_harness_rejects_non_localhost_before_starting_obs() -> None:
    with pytest.raises(HarnessFailure, match="PREP_LIVE_DISABLED"):
        run_live_harness(
            executable="not-started",
            profile="W11-010",
            collection="W11-010",
            host="192.0.2.10",
            port=4455,
            password="unused",
            scene="scene",
            source="source",
            zoom_config=ZoomConfig(level=2.0, deadzone=0.0, smoothing=1.0),
            timeout=1.0,
        )
