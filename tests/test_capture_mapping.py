from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

import obs_voice_command.runtime as runtime_module
from obs_voice_command.main import main
from obs_voice_command.obs_client import CaptureMapping, TransformContractError
from obs_voice_command.platform.common import DisplayInfo
from obs_voice_command.runtime import (
    Runtime,
    RuntimeDependencies,
    RuntimeStartupError,
    _capture_controller_values,
)
from obs_voice_command.zoom import Transform


CAPTURE_DISPLAY = DisplayInfo(
    origin_x=-1280,
    origin_y=0,
    width_pts=1280,
    height_pts=720,
    width_px=1280,
    height_px=720,
    id="capture-display",
    aliases=(r"\\.\DISPLAY2",),
)
OTHER_DISPLAY = DisplayInfo(
    origin_x=0,
    origin_y=0,
    width_pts=1920,
    height_pts=1080,
    width_px=1920,
    height_px=1080,
    id="other-display",
)
ORIGINAL = Transform(pos_x=3, pos_y=4, scale_x=1.0, scale_y=1.0)


def _monitor_mapping() -> CaptureMapping:
    return CaptureMapping(
        display=CAPTURE_DISPLAY,
        source_width=2560,
        source_height=1440,
        display_to_source_scale_x=2.0,
        display_to_source_scale_y=2.0,
    )


def _item(*, input_kind="monitor_capture", mapping=None):
    return SimpleNamespace(
        source_width=2560.0,
        source_height=1440.0,
        input_kind=input_kind,
        capture_mapping=mapping,
    )


def _args(**overrides):
    values = {
        "config": "missing-config.toml",
        "dry_run": False,
        "os": False,
        "list_devices": False,
    }
    values.update(overrides)
    return Namespace(**values)


def test_mapped_capture_restricts_controller_to_display_and_preserves_scale():
    displays = [OTHER_DISPLAY, CAPTURE_DISPLAY]

    controller_displays, selected, scale = _capture_controller_values(
        _item(mapping=_monitor_mapping()), displays, (2560.0, 1440.0)
    )

    assert controller_displays == [CAPTURE_DISPLAY]
    assert selected == CAPTURE_DISPLAY
    assert scale == (2.0, 2.0)


def test_monitor_capture_without_identity_mapping_fails_closed():
    with pytest.raises(RuntimeStartupError, match="identity was not mapped"):
        _capture_controller_values(
            _item(), [CAPTURE_DISPLAY], (2560.0, 1440.0)
        )


def test_mac_compatible_display_capture_keeps_existing_display_flow():
    displays = [CAPTURE_DISPLAY, OTHER_DISPLAY]

    controller_displays, selected, scale = _capture_controller_values(
        _item(input_kind="display_capture"), displays, (2560.0, 1440.0)
    )

    assert controller_displays is displays
    assert selected is None
    assert scale is None


class FakePointer:
    def get_displays(self):
        return [OTHER_DISPLAY, CAPTURE_DISPLAY]

    def get_cursor_position(self):
        return (-640.0, 360.0)

    def locate(self, position, displays):
        del position
        return displays[-1], 640.0, 360.0


class WiringObs:
    def __init__(self, item, *, validation_error=None):
        self.item = item
        self.validation_error = validation_error
        self.find_calls = []
        self.validation_calls = []
        self.set_calls = []
        self.connected = False
        self.closed = False

    def connect(self):
        self.connected = True

    def find_display_capture(self, scene, source, *, displays, canvas):
        self.find_calls.append((scene, source, list(displays), canvas))
        return self.item

    def get_canvas_size(self):
        return (2560.0, 1440.0)

    def get_transform(self, item):
        assert item is self.item
        return ORIGINAL

    def validate_transform_contract(self, item, canvas):
        self.validation_calls.append((item, canvas))
        if self.validation_error is not None:
            raise self.validation_error

    def set_transform(self, item, transform):
        self.set_calls.append((item, transform))

    def close(self):
        self.closed = True


class InterruptingStream:
    def __init__(self):
        self.closed = False

    def __enter__(self):
        raise KeyboardInterrupt()

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def close(self):
        self.closed = True


class FakeAsr:
    def feed(self, samples):
        del samples
        return "", False


def test_runtime_wires_capture_identity_scale_before_audio_start(monkeypatch):
    item = _item(mapping=_monitor_mapping())
    obs = WiringObs(item)
    stream = InterruptingStream()
    created_controllers = []

    class SpyController:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.started = False
            self.stopped = False
            created_controllers.append(self)

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(runtime_module, "ZoomController", SpyController)
    dependencies = RuntimeDependencies(
        model_factory=lambda: Path("fake-model"),
        asr_factory=lambda model_dir: FakeAsr(),
        pointer_factory=lambda: FakePointer(),
        obs_factory=lambda config: obs,
        audio_factory=lambda config, callback: stream,
        clock=lambda: 0.0,
        wait=lambda seconds: None,
    )

    Runtime(_args(), dependencies).run()

    assert obs.connected is True
    assert obs.find_calls == [
        ("", "", [OTHER_DISPLAY, CAPTURE_DISPLAY], (2560.0, 1440.0))
    ]
    assert obs.validation_calls == [(item, (2560.0, 1440.0))]
    assert len(created_controllers) == 1
    controller = created_controllers[0]
    assert controller.args[6] == [CAPTURE_DISPLAY]
    assert controller.kwargs["capture_display"] == CAPTURE_DISPLAY
    assert controller.kwargs["display_to_source_scale"] == (2.0, 2.0)
    assert controller.started is False
    assert controller.stopped is True
    assert stream.closed is True
    assert obs.closed is True
    assert obs.set_calls == []


def test_runtime_rejects_bad_geometry_before_audio_or_transform_writes():
    item = _item(mapping=_monitor_mapping())
    obs = WiringObs(item, validation_error=TransformContractError("bad geometry"))
    audio_calls = []

    def audio_factory(config, callback):
        del config, callback
        audio_calls.append(True)
        raise AssertionError("audio must not start after geometry rejection")

    dependencies = RuntimeDependencies(
        model_factory=lambda: Path("fake-model"),
        asr_factory=lambda model_dir: FakeAsr(),
        pointer_factory=lambda: FakePointer(),
        obs_factory=lambda config: obs,
        audio_factory=audio_factory,
    )

    with pytest.raises(SystemExit) as raised:
        main(_args(), dependencies=dependencies)

    assert raised.value.code == 1
    assert audio_calls == []
    assert obs.set_calls == []
    assert obs.closed is True
