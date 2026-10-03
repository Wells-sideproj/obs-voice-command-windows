from __future__ import annotations

import time

import pytest

from tools import windows11_integration_harness as harness_module
from obs_voice_command.config import ZoomConfig
from obs_voice_command.platform.common import DisplayInfo
from obs_voice_command.zoom import Transform
from tools.windows11_integration_harness import (
    HarnessFailure,
    assert_endpoint_free,
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
        sleep=time.sleep,
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
    with pytest.raises(HarnessFailure, match="cleanup") as raised:
        _run(FakeObs(fail_cleanup=True))

    assert raised.value.cleanup_error is not None
    assert "baseline cleanup failed" in raised.value.cleanup_error


def test_nested_harness_cleanup_error_is_preserved() -> None:
    def fail_inside_controller(**kwargs):
        del kwargs
        raise HarnessFailure(
            "inner harness failure",
            cleanup_error="inner baseline cleanup failed",
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
            sleep=time.sleep,
            controller_factory=fail_inside_controller,
        )

    assert raised.value.cleanup_error == "inner harness cleanup_error: inner baseline cleanup failed"
    assert "inner baseline cleanup failed" in str(raised.value)


class FakeLiveObs:
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


def test_live_harness_keeps_inner_obs_and_process_cleanup_errors_separate(monkeypatch) -> None:
    inner_failure = HarnessFailure(
        "inner transform failure",
        cleanup_error="inner baseline cleanup failed",
    )

    def fail_transform(**kwargs):
        del kwargs
        raise inner_failure

    monkeypatch.setattr(harness_module, "assert_endpoint_free", lambda host, port: None)
    fake_process = type("Process", (), {"poll": lambda self: None})()
    monkeypatch.setattr(
        harness_module,
        "_start_obs",
        lambda executable, profile, collection: fake_process,
    )
    monkeypatch.setattr(harness_module, "wait_for_endpoint", lambda host, port, timeout: None)
    monkeypatch.setattr(harness_module, "ObsClient", lambda host, port, password: FakeLiveObs())
    monkeypatch.setattr(harness_module, "SystemPointer", lambda: FakePointer())
    monkeypatch.setattr(harness_module, "run_transform_harness", fail_transform)
    monkeypatch.setattr(
        harness_module,
        "_close_obs",
        lambda obs: "OBS disconnect failed: RuntimeError",
    )
    monkeypatch.setattr(
        harness_module,
        "_stop_owned_process",
        lambda process: "owned OBS cleanup failed: TimeoutExpired",
    )

    with pytest.raises(HarnessFailure) as raised:
        run_live_harness(
            executable="fake-obs",
            profile="W11-010",
            collection="W11-010",
            host="127.0.0.1",
            port=4455,
            password="unused",
            scene="scene",
            source="source",
            zoom_config=ZoomConfig(level=2.0, deadzone=0.0, smoothing=1.0),
            timeout=1.0,
        )

    assert raised.value.cleanup_error is not None
    assert "inner harness cleanup_error: inner baseline cleanup failed" in raised.value.cleanup_error
    assert "OBS disconnect failed: RuntimeError" in raised.value.cleanup_error
    assert "owned OBS cleanup failed: TimeoutExpired" in raised.value.cleanup_error


class OccupiedConnection:
    def close(self):
        return None


def test_live_preflight_fails_closed_when_obs_port_is_occupied() -> None:
    with pytest.raises(HarnessFailure, match="already occupied"):
        assert_endpoint_free(
            "127.0.0.1",
            4455,
            connect=lambda address, timeout: OccupiedConnection(),
        )


def test_live_harness_rejects_non_localhost_before_starting_obs() -> None:
    with pytest.raises(HarnessFailure, match="hard-locked"):
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
