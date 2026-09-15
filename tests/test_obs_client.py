from __future__ import annotations

from types import SimpleNamespace

import pytest
from obsws_python.util import as_dataclass

from obs_voice_command.obs_client import (
    CAPTURE_INPUT_KINDS,
    ObsClient,
    TransformContractError,
)
from obs_voice_command.platform.common import DisplayInfo


DISPLAY = DisplayInfo(
    origin_x=-1920.0,
    origin_y=-100.0,
    width_pts=1920.0,
    height_pts=1080.0,
    width_px=1920,
    height_px=1080,
    id="stable-monitor-id",
    aliases=(r"\\.\DISPLAY2",),
)


def _transform(**overrides):
    value = {
        "positionX": 0.0,
        "positionY": 0.0,
        "scaleX": 1.0,
        "scaleY": 1.0,
        "rotation": 0.0,
        "cropLeft": 0,
        "cropRight": 0,
        "cropTop": 0,
        "cropBottom": 0,
        "sourceWidth": 1920.0,
        "sourceHeight": 1080.0,
        "width": 1920.0,
        "height": 1080.0,
        "boundsType": 0,
        "boundsWidth": 0.0,
        "boundsHeight": 0.0,
        "boundsAlignment": 0,
        "alignment": 5,
    }
    value.update(overrides)
    return value


class FakeObsWs:
    def __init__(
        self,
        *,
        scene_response=None,
        items=None,
        settings=None,
        transform=None,
    ):
        self.scene_response = scene_response or {"sceneName": "Current Scene"}
        self.items = list(items or [])
        self.settings = dict(settings or {})
        self.transform = dict(transform or _transform())
        self.scene_requests: list[str] = []
        self.input_requests: list[str] = []
        self.transform_requests: list[tuple[str, int]] = []

    def get_current_program_scene(self):
        return self.scene_response

    def get_scene_item_list(self, scene_name):
        self.scene_requests.append(scene_name)
        return {"sceneItems": self.items}

    def get_input_settings(self, source_name):
        self.input_requests.append(source_name)
        return {"inputSettings": self.settings}

    def get_scene_item_transform(self, scene_name, item_id):
        self.transform_requests.append((scene_name, item_id))
        return {"sceneItemTransform": self.transform}

    def get_video_settings(self):
        return SimpleNamespace(base_width=1920, base_height=1080)


def _client(fake: FakeObsWs) -> ObsClient:
    client = ObsClient("127.0.0.1", 4455, "fixture-password")
    client._client = fake
    return client


def _item(name: str, kind: str, item_id: int = 7):
    return {
        "sourceName": name,
        "sceneItemId": item_id,
        "inputKind": kind,
        "sourceType": "input",
    }


@pytest.mark.parametrize("kind", sorted(CAPTURE_INPUT_KINDS))
def test_supported_capture_kinds_are_selected(kind):
    settings = {"monitor_id": "stable-monitor-id"} if kind == "monitor_capture" else {}
    fake = FakeObsWs(items=[_item("Capture", kind)], settings=settings)

    item = _client(fake).find_display_capture(
        "Current Scene", "", displays=[DISPLAY], canvas=(1920, 1080)
    )

    assert item.source_name == "Capture"
    assert item.input_kind == kind
    if kind == "monitor_capture":
        assert item.capture_display == DISPLAY
        assert item.display_to_source_scale == (1.0, 1.0)
        assert fake.input_requests == ["Capture"]
    else:
        assert item.capture_display is None
        assert fake.input_requests == []


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"sceneName": "Current"}, "Current"),
        (
            {
                "sceneName": "Current",
                "currentProgramSceneName": "Deprecated",
            },
            "Current",
        ),
        ({"currentProgramSceneName": "Deprecated"}, "Deprecated"),
        (SimpleNamespace(scene_name="Snake current"), "Snake current"),
        (
            as_dataclass(
                "GetCurrentProgramScene",
                {"currentProgramSceneName": "Snake deprecated"},
            )(),
            "Snake deprecated",
        ),
    ],
)
def test_current_program_scene_supports_current_and_legacy_response_shapes(
    response, expected
):
    fake = FakeObsWs(scene_response=response, items=[_item("Capture", "display_capture")])

    item = _client(fake).find_display_capture(
        "", "", displays=[DISPLAY], canvas=(1920, 1080)
    )

    assert item.scene_name == expected
    assert fake.scene_requests == [expected]


def test_no_supported_capture_is_reported_with_available_sources():
    fake = FakeObsWs(items=[_item("Camera", "camera_input")])

    with pytest.raises(RuntimeError, match="No supported OBS capture source") as raised:
        _client(fake).find_display_capture("Current Scene", "")

    assert "Camera" in str(raised.value)
    assert fake.transform_requests == []


def test_multiple_captures_require_an_exact_source_and_exact_source_wins():
    fake = FakeObsWs(
        items=[_item("Display A", "monitor_capture", 1), _item("Display B", "screen_capture", 2)]
    )
    client = _client(fake)

    with pytest.raises(RuntimeError, match="Multiple supported OBS capture sources") as raised:
        client.find_display_capture("Current Scene", "")
    assert "Display A" in str(raised.value)
    assert "Display B" in str(raised.value)

    selected = client.find_display_capture("Current Scene", "Display B")
    assert selected.source_name == "Display B"
    assert selected.input_kind == "screen_capture"


def test_explicit_source_must_be_unique_and_supported():
    duplicate = FakeObsWs(
        items=[_item("Same", "display_capture", 1), _item("Same", "camera_input", 2)]
    )
    with pytest.raises(RuntimeError, match="exact, unique supported capture"):
        _client(duplicate).find_display_capture("Current Scene", "Same")

    unsupported = FakeObsWs(items=[_item("Camera", "camera_input")])
    with pytest.raises(RuntimeError, match="unsupported input kind"):
        _client(unsupported).find_display_capture("Current Scene", "Camera")


@pytest.mark.parametrize("identity", ["stable-monitor-id", r"\\.\DISPLAY2"])
def test_monitor_id_matches_only_display_id_or_exact_alias(identity):
    fake = FakeObsWs(
        items=[_item("Monitor", "monitor_capture")],
        settings={"monitor_id": identity},
    )

    item = _client(fake).find_display_capture(
        "Current Scene", "", displays=[DISPLAY], canvas=(1920, 1080)
    )

    assert item.capture_display == DISPLAY
    assert item.monitor_id == identity


def test_unknown_monitor_id_fails_with_sanitized_context():
    fake = FakeObsWs(
        items=[_item("Private capture", "monitor_capture")],
        settings={
            "monitor_id": "private-monitor-identity",
            "password": "secret-password",
            "token": "secret-token",
        },
    )

    with pytest.raises(RuntimeError, match="Unable to map OBS capture monitor") as raised:
        _client(fake).find_display_capture(
            "Current Scene", "", displays=[DISPLAY], canvas=(1920, 1080)
        )

    message = str(raised.value)
    assert "private-monitor-identity" not in message
    assert "secret-password" not in message
    assert "secret-token" not in message
    assert "Available display IDs" in message
    assert "stable-monitor-id" in message


def test_missing_monitor_id_includes_available_display_ids_without_leaking_settings():
    fake = FakeObsWs(
        items=[_item("Missing identity", "monitor_capture")],
        settings={
            "password": "secret-password",
            "token": "secret-token",
        },
    )

    with pytest.raises(RuntimeError, match="has no monitor_id") as raised:
        _client(fake).find_display_capture(
            "Current Scene", "", displays=[DISPLAY], canvas=(1920, 1080)
        )

    message = str(raised.value)
    assert "Available display IDs" in message
    assert "stable-monitor-id" in message
    assert "secret-password" not in message
    assert "secret-token" not in message


def test_ambiguous_monitor_id_fails_without_resolution_guess():
    second = DisplayInfo(
        origin_x=0.0,
        origin_y=0.0,
        width_pts=1920.0,
        height_pts=1080.0,
        width_px=1920,
        height_px=1080,
        id="another-monitor-id",
        aliases=(r"\\.\DISPLAY2",),
    )
    fake = FakeObsWs(
        items=[_item("Monitor", "monitor_capture")],
        settings={"monitor_id": r"\\.\DISPLAY2"},
    )

    with pytest.raises(RuntimeError, match="ambiguous"):
        _client(fake).find_display_capture(
            "Current Scene", "", displays=[DISPLAY, second], canvas=(1920, 1080)
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"sourceWidth": None},
        {"scaleX": float("nan")},
        {"scaleX": 0.0},
        {"scaleY": 1.1},
        {"rotation": 90.0},
        {"cropLeft": 1.0},
        {"cropTop": -1.0},
        {"width": 1919.0},
        {"height": 1079.0},
        {"boundsType": 1},
        {"boundsWidth": 1.0},
        {"boundsAlignment": 1},
        {"positionX": 1.0},
        {"alignment": 0},
        {"alignment": 6},
    ],
)
def test_invalid_or_incompatible_transform_is_rejected_before_mapping(overrides):
    transform = _transform(**overrides)
    fake = FakeObsWs(
        items=[_item("Monitor", "monitor_capture")],
        settings={"monitor_id": "stable-monitor-id"},
        transform=transform,
    )

    with pytest.raises(TransformContractError):
        _client(fake).find_display_capture(
            "Current Scene", "", displays=[DISPLAY], canvas=(1920, 1080)
        )


@pytest.mark.parametrize("field", ["width", "height", "boundsType", "boundsWidth", "boundsHeight", "boundsAlignment", "alignment"])
def test_required_full_canvas_geometry_fields_cannot_be_defaulted(field):
    transform = _transform()
    transform.pop(field)
    fake = FakeObsWs(
        items=[_item("Display", "display_capture")],
        transform=transform,
    )

    with pytest.raises(TransformContractError, match="missing"):
        _client(fake).find_display_capture(
            "Current Scene", "", displays=[DISPLAY], canvas=(1920, 1080)
        )


def test_valid_full_canvas_transform_and_snake_case_response_fields_are_accepted():
    transform = {
        "position_x": 0.0,
        "position_y": 0.0,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "rotation": 0.0,
        "crop_left": 0,
        "crop_right": 0,
        "crop_top": 0,
        "crop_bottom": 0,
        "source_width": 1920.0,
        "source_height": 1080.0,
        "width": 1920.0,
        "height": 1080.0,
        "bounds_type": 0,
        "bounds_width": 0.0,
        "bounds_height": 0.0,
        "bounds_alignment": 0,
        "alignment": 5,
    }
    fake = FakeObsWs(
        scene_response=SimpleNamespace(current_program_scene_name="Current"),
        items=[
            {
                "source_name": "Display",
                "scene_item_id": 7,
                "input_kind": "display_capture",
            }
        ],
        transform=transform,
    )

    item = _client(fake).find_display_capture(
        "", "", displays=[DISPLAY], canvas=(1920, 1080)
    )

    assert item.scene_name == "Current"
    assert item.source_width == 1920.0
    assert item.source_height == 1080.0
