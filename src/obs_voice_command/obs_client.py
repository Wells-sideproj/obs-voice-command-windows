"""OBS WebSocket client wrapper for zoom control and capture mapping."""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from types import MappingProxyType
from typing import Any

from obsws_python import ReqClient
from obsws_python.error import OBSSDKError

from .platform.common import DisplayInfo
from .zoom import Transform


CAPTURE_INPUT_KINDS = frozenset(
    {"monitor_capture", "display_capture", "screen_capture"}
)
MONITOR_CAPTURE_KIND = "monitor_capture"

_MISSING = object()
_SENSITIVE_KEY_RE = re.compile(
    r"(?:pass(?:word|wd)?|secret|token|authorization|credential|"
    r"api[_-]?key|private[_-]?key|cookie)",
    re.IGNORECASE,
)
_IDENTITY_KEY_NAMES = {"monitorid", "monitoridentifier"}
_GEOMETRY_TOLERANCE = 1e-4
_TOP_LEFT_ALIGNMENT = 1 | 4


class TransformContractError(RuntimeError):
    """The selected OBS source cannot be represented safely by zoom math."""


def _normalise_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _as_mapping(value: Any) -> Mapping[Any, Any] | None:
    if isinstance(value, Mapping):
        return value
    asdict = getattr(value, "_asdict", None)
    if callable(asdict):
        try:
            return asdict()
        except Exception:
            return None
    try:
        return vars(value)
    except TypeError:
        return None


def _field(value: Any, *names: str, default: Any = _MISSING) -> Any:
    """Read response fields from obsws-python objects or fake mappings."""

    mapping = _as_mapping(value)
    if mapping is not None:
        normalised = {_normalise_key(key): child for key, child in mapping.items()}
        for name in names:
            found = normalised.get(_normalise_key(name), _MISSING)
            if found is not _MISSING:
                return found

    for name in names:
        try:
            return getattr(value, name)
        except AttributeError:
            continue
    return default


def _safe_text(value: Any, *, limit: int = 240, secrets: tuple[str, ...] = ()) -> str:
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    text = re.sub(
        r"(?P<key>pass(?:word|wd)?|secret|token|authorization|credential|"
        r"api[_-]?key)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|\S+)",
        lambda match: f"{match.group('key')}=<redacted>",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\bBearer\s+\S+", "Bearer <redacted>", text, flags=re.IGNORECASE)
    text = "".join(char if char.isprintable() else "?" for char in text)
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _safe_setting(value: Any, *, key: str | None = None) -> Any:
    """Return a bounded settings summary without credential-like values."""

    if key is not None and _SENSITIVE_KEY_RE.search(key):
        return "<redacted>"

    normalised_key = _normalise_key(key or "")
    if normalised_key in _IDENTITY_KEY_NAMES:
        if value in (None, ""):
            return {"present": False, "redacted": True}
        digest = sha256(str(value).encode("utf-8", errors="replace")).hexdigest()[:12]
        return {"present": True, "redacted": True, "sha256_12": digest}

    if isinstance(value, Mapping):
        return {
            str(child_key): _safe_setting(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_setting(child) for child in value]
    if isinstance(value, bytes):
        return f"<bytes length={len(value)}>"
    if isinstance(value, str):
        return _safe_text(value, limit=160)
    if isinstance(value, float) and not math.isfinite(value):
        return f"<{value!r}>"

    # obsws-python returns top-level responses as generated dataclasses, while
    # lightweight fakes often use SimpleNamespace.  Treat both as mappings so
    # nested monitor/password values cannot fall through to repr() unsanitized.
    object_mapping = _as_mapping(value)
    if object_mapping is not None:
        return {
            str(child_key): _safe_setting(child_value, key=str(child_key))
            for child_key, child_value in object_mapping.items()
        }
    return value


def _settings_summary(settings: Any) -> str:
    try:
        encoded = json.dumps(
            _safe_setting(settings),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        encoded = _safe_text(settings)
    return _safe_text(encoded, limit=320)


def _available_display_ids(displays: list[DisplayInfo]) -> str:
    identifiers: list[str] = []
    for display in displays:
        for identifier in (display.id, *display.aliases):
            if identifier and identifier not in identifiers:
                identifiers.append(identifier)
    return ", ".join(
        _safe_text(identifier, limit=160) for identifier in identifiers
    ) or "<none>"


def _number(
    value: Any,
    field_name: str,
    *,
    positive: bool = False,
    default: float | object = _MISSING,
) -> float:
    if value is _MISSING or value is None or value == "":
        if default is _MISSING:
            raise TransformContractError(
                f"OBS transform field {field_name!r} is missing"
            )
        value = default
    try:
        converted = float(value)
    except (TypeError, ValueError) as exc:
        raise TransformContractError(
            f"OBS transform field {field_name!r} is not numeric"
        ) from exc
    if not math.isfinite(converted):
        raise TransformContractError(
            f"OBS transform field {field_name!r} must be finite"
        )
    if positive and converted <= 0:
        raise TransformContractError(
            f"OBS transform field {field_name!r} must be positive"
        )
    return converted


def _number_from(
    value: Any,
    names: tuple[str, ...],
    field_name: str,
    *,
    positive: bool = False,
    default: float | object = _MISSING,
) -> float:
    return _number(
        _field(value, *names, default=_MISSING),
        field_name,
        positive=positive,
        default=default,
    )


def _normalise_kind(value: Any) -> str:
    if value is _MISSING or value is None:
        return ""
    return str(value).strip().lower().replace("-", "_")


def _is_close(left: float, right: float) -> bool:
    return math.isclose(
        left,
        right,
        rel_tol=_GEOMETRY_TOLERANCE,
        abs_tol=_GEOMETRY_TOLERANCE,
    )


def _normalise_bounds_type(value: Any) -> str | int:
    if value is _MISSING or value is None or value == "":
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        number = _number(value, "boundsType")
        if number.is_integer():
            return int(number)
        return number
    return _normalise_kind(value)


def _alignment_value(value: Any) -> int:
    if value is _MISSING or value is None or value == "":
        return 0
    if isinstance(value, str):
        normalised = _normalise_kind(value)
        if normalised in {"default", "obs_align_default"}:
            return 0
        try:
            value = int(value, 10)
        except ValueError as exc:
            raise TransformContractError(
                f"OBS transform alignment {value!r} is unsupported"
            ) from exc
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TransformContractError("OBS transform alignment is not numeric") from exc
    if not math.isfinite(number) or not number.is_integer():
        raise TransformContractError("OBS transform alignment must be finite integer")
    alignment = int(number)
    if alignment < 0 or alignment > 15:
        raise TransformContractError(
            f"OBS transform alignment {alignment!r} is unsupported"
        )
    return alignment


def _validate_geometry(
    transform_data: Any,
    *,
    source_width: float,
    source_height: float,
    scale_x: float,
    scale_y: float,
    canvas: tuple[float, float],
    strict: bool,
) -> None:
    """Validate geometry that the existing top-left zoom math can preserve."""

    canvas_width = _number(canvas[0], "canvas width", positive=True)
    canvas_height = _number(canvas[1], "canvas height", positive=True)

    width = _number_from(
        transform_data,
        ("width",),
        "width",
        positive=True,
        default=(_MISSING if strict else source_width * scale_x),
    )
    height = _number_from(
        transform_data,
        ("height",),
        "height",
        positive=True,
        default=(_MISSING if strict else source_height * scale_y),
    )
    if not _is_close(width, source_width * scale_x) or not _is_close(
        height, source_height * scale_y
    ):
        raise TransformContractError(
            "OBS transform width/height do not match source dimensions and scale"
        )
    if not _is_close(width, canvas_width) or not _is_close(height, canvas_height):
        raise TransformContractError(
            "OBS source does not fill the full canvas: "
            f"source geometry {width:g}x{height:g}, "
            f"canvas {canvas_width:g}x{canvas_height:g}"
        )

    bounds_type_raw = _field(
        transform_data, "boundsType", "bounds_type", default=_MISSING
    )
    if strict and bounds_type_raw is _MISSING:
        raise TransformContractError("OBS transform field 'boundsType' is missing")
    bounds_type = _normalise_bounds_type(bounds_type_raw)
    if bounds_type not in {0, "none", "obs_bounds_none"}:
        raise TransformContractError(
            f"OBS transform boundsType {bounds_type!r} is unsupported; bounds must be none"
        )

    for field_name, names in (
        ("boundsWidth", ("boundsWidth", "bounds_width")),
        ("boundsHeight", ("boundsHeight", "bounds_height")),
    ):
        value = _field(transform_data, *names, default=_MISSING)
        if strict and value is _MISSING:
            raise TransformContractError(f"OBS transform field {field_name!r} is missing")
        if value is _MISSING:
            continue
        bounds_dimension = _number(value, field_name)
        if not _is_close(bounds_dimension, 0.0):
            raise TransformContractError(
                f"OBS transform {field_name} must be zero when bounds are disabled"
            )

    bounds_alignment = _field(
        transform_data,
        "boundsAlignment",
        "bounds_alignment",
        default=_MISSING,
    )
    if strict and bounds_alignment is _MISSING:
        raise TransformContractError(
            "OBS transform field 'boundsAlignment' is missing"
        )
    if bounds_alignment is not _MISSING and _alignment_value(bounds_alignment) != 0:
        raise TransformContractError(
            "OBS transform boundsAlignment is unsupported when bounds are disabled"
        )

    position_x = _number_from(
        transform_data,
        ("positionX", "position_x"),
        "positionX",
        default=0.0,
    )
    position_y = _number_from(
        transform_data,
        ("positionY", "position_y"),
        "positionY",
        default=0.0,
    )
    alignment_raw = _field(transform_data, "alignment", default=_MISSING)
    if strict and alignment_raw is _MISSING:
        raise TransformContractError("OBS transform field 'alignment' is missing")
    if alignment_raw is not _MISSING and _alignment_value(alignment_raw) != _TOP_LEFT_ALIGNMENT:
        # compute_transform() writes top-left-origin positions. A centered or
        # right/bottom-aligned item may currently cover the canvas but would
        # be moved incorrectly by the next zoom write, so reject it.
        raise TransformContractError(
            "OBS transform alignment is incompatible with top-left zoom math; "
            "only left/top alignment is supported"
        )
    if not _is_close(position_x, 0.0) or not _is_close(position_y, 0.0):
        raise TransformContractError(
            "OBS source is not positioned at the top-left of the full canvas: "
            f"position=({position_x:g},{position_y:g})"
        )


def validate_raw_transform(
    transform_data: Any,
    *,
    source_size: tuple[float, float] | None = None,
    canvas: tuple[float, float] | None = None,
    strict: bool = False,
) -> Transform:
    """Validate and parse an OBS scene-item transform response.

    ``strict=True`` is used for live ``ObsClient`` responses and requires the
    complete OBS transform contract. The default compatibility mode is kept
    for callers that parse the small position/scale subset used by old fakes.
    """

    if _as_mapping(transform_data) is None:
        raise TransformContractError(
            "GetSceneItemTransform did not return a sceneItemTransform object"
        )

    source_width = _number_from(
        transform_data,
        ("sourceWidth", "source_width"),
        "sourceWidth",
        positive=True,
        default=(
            _MISSING
            if strict or source_size is None
            else source_size[0]
        ),
    )
    source_height = _number_from(
        transform_data,
        ("sourceHeight", "source_height"),
        "sourceHeight",
        positive=True,
        default=(
            _MISSING
            if strict or source_size is None
            else source_size[1]
        ),
    )
    if source_size is not None:
        expected_width = _number(source_size[0], "source width", positive=True)
        expected_height = _number(source_size[1], "source height", positive=True)
        if not _is_close(source_width, expected_width) or not _is_close(
            source_height, expected_height
        ):
            raise TransformContractError(
                "OBS source dimensions changed while the scene item was selected"
            )

    position_x = _number_from(
        transform_data,
        ("positionX", "position_x"),
        "positionX",
        default=(_MISSING if strict else 0.0),
    )
    position_y = _number_from(
        transform_data,
        ("positionY", "position_y"),
        "positionY",
        default=(_MISSING if strict else 0.0),
    )
    scale_x = _number_from(
        transform_data,
        ("scaleX", "scale_x"),
        "scaleX",
        positive=True,
        default=(_MISSING if strict else 1.0),
    )
    scale_y = _number_from(
        transform_data,
        ("scaleY", "scale_y"),
        "scaleY",
        positive=True,
        default=(_MISSING if strict else 1.0),
    )
    if not _is_close(scale_x, scale_y):
        raise TransformContractError(
            f"OBS transform must use uniform scale, got scaleX={scale_x:g}, scaleY={scale_y:g}"
        )

    rotation = _number_from(
        transform_data,
        ("rotation",),
        "rotation",
        default=(_MISSING if strict else 0.0),
    )
    if not _is_close(rotation, 0.0):
        raise TransformContractError(
            f"OBS transform rotation {rotation:g} is unsupported; expected zero"
        )

    for field_name, names in (
        ("cropLeft", ("cropLeft", "crop_left")),
        ("cropRight", ("cropRight", "crop_right")),
        ("cropTop", ("cropTop", "crop_top")),
        ("cropBottom", ("cropBottom", "crop_bottom")),
    ):
        crop = _number_from(
            transform_data,
            names,
            field_name,
            default=(_MISSING if strict else 0.0),
        )
        if not _is_close(crop, 0.0):
            raise TransformContractError(
                f"OBS transform {field_name} {crop:g} is unsupported; crop must be zero"
            )

    if canvas is not None:
        _validate_geometry(
            transform_data,
            source_width=source_width,
            source_height=source_height,
            scale_x=scale_x,
            scale_y=scale_y,
            canvas=canvas,
            strict=strict,
        )

    return Transform(
        pos_x=position_x,
        pos_y=position_y,
        scale_x=scale_x,
        scale_y=scale_y,
    )


def _validate_outgoing_transform(transform: Transform) -> None:
    _number(transform.pos_x, "positionX")
    _number(transform.pos_y, "positionY")
    scale_x = _number(transform.scale_x, "scaleX", positive=True)
    scale_y = _number(transform.scale_y, "scaleY", positive=True)
    if not _is_close(scale_x, scale_y):
        raise TransformContractError("outgoing OBS transform must use uniform scale")


@dataclass(frozen=True)
class CaptureMapping:
    """Map physical display pixels to the selected OBS source pixels."""

    display: DisplayInfo
    source_width: float
    source_height: float
    display_to_source_scale_x: float
    display_to_source_scale_y: float

    def __post_init__(self) -> None:
        _number(self.source_width, "source width", positive=True)
        _number(self.source_height, "source height", positive=True)
        _number(
            self.display_to_source_scale_x,
            "display-to-source scaleX",
            positive=True,
        )
        _number(
            self.display_to_source_scale_y,
            "display-to-source scaleY",
            positive=True,
        )

    @property
    def scale_x(self) -> float:
        return self.display_to_source_scale_x

    @property
    def scale_y(self) -> float:
        return self.display_to_source_scale_y

    def to_source_pixels(self, x: float, y: float) -> tuple[float, float]:
        return (
            _number(x, "display pixel x") * self.display_to_source_scale_x,
            _number(y, "display pixel y") * self.display_to_source_scale_y,
        )


@dataclass(frozen=True)
class SceneItem:
    """Selected scene item and its optional capture-display mapping."""

    scene_name: str
    item_id: int
    source_width: float
    source_height: float
    source_name: str = ""
    input_kind: str = ""
    monitor_id: str | None = field(default=None, repr=False, compare=False)
    capture_display: DisplayInfo | None = field(default=None, compare=False)
    display_to_source_scale_x: float = field(default=1.0, compare=False)
    display_to_source_scale_y: float = field(default=1.0, compare=False)
    capture_mapping: CaptureMapping | None = field(default=None, compare=False)
    _raw_transform: Mapping[str, Any] | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def selected_display(self) -> DisplayInfo | None:
        return self.capture_display

    @property
    def display_to_source_scale(self) -> tuple[float, float]:
        return (
            self.display_to_source_scale_x,
            self.display_to_source_scale_y,
        )


class ObsClient:
    """OBS WebSocket client with zoom-control-specific methods.

    Connection is deferred until connect() is called to allow
    graceful error handling and reconnection logic in main.py.
    """

    def __init__(self, host: str, port: int, password: str) -> None:
        """Store connection parameters for later connection."""
        self.host = host
        self.port = port
        self.password = password
        self._client: ReqClient | None = None

    def connect(self) -> None:
        """Create ReqClient and authenticate with OBS."""
        try:
            self._client = ReqClient(
                host=self.host, port=self.port, password=self.password
            )
        except (OBSSDKError, ConnectionRefusedError, TimeoutError, OSError) as exc:
            detail = _safe_text(exc, secrets=(self.password,))
            raise ConnectionError(
                f"Failed to connect to OBS at {self.host}:{self.port}. "
                "Ensure: (1) OBS is running, (2) WebSocket server is enabled in "
                f"Tools → WebSocket Server Settings, (3) password is correct. Error: {detail}"
            ) from exc

    def _require_client(self) -> ReqClient:
        if self._client is None:
            raise RuntimeError("Not connected. Call connect() first.")
        return self._client

    def get_canvas_size(self) -> tuple[float, float]:
        """Get canvas (base resolution) size in pixels.

        Returns:
            Tuple of (width, height).
        """
        video_settings = self._require_client().get_video_settings()
        width = _number_from(
            video_settings,
            ("baseWidth", "base_width"),
            "canvas width",
            positive=True,
        )
        height = _number_from(
            video_settings,
            ("baseHeight", "base_height"),
            "canvas height",
            positive=True,
        )
        return width, height

    def _resolve_scene_name(self, scene: str) -> str:
        if scene:
            return scene
        scene_result = self._require_client().get_current_program_scene()
        # OBS 5.x currently documents sceneName and retains the older
        # currentProgramSceneName field for compatibility.  obsws-python's
        # as_dataclass converts either camel-case response to snake_case.
        for field_names in (
            ("sceneName", "scene_name"),
            ("currentProgramSceneName", "current_program_scene_name"),
        ):
            resolved = _field(scene_result, *field_names, default=_MISSING)
            if resolved is not _MISSING and str(resolved).strip():
                return str(resolved)
        raise RuntimeError("OBS did not return a current program scene name")

    def _scene_items(self, scene: str) -> list[Any]:
        items_result = self._require_client().get_scene_item_list(scene)
        items = _field(items_result, "sceneItems", "scene_items", default=_MISSING)
        if items is _MISSING or not isinstance(items, (list, tuple)):
            raise RuntimeError(
                f"OBS did not return a scene item list for scene {_safe_text(scene)!r}"
            )
        return list(items)

    def _read_transform_data(self, scene: str, item_id: int) -> dict[str, Any]:
        result = self._require_client().get_scene_item_transform(scene, item_id)
        data = _field(
            result,
            "sceneItemTransform",
            "scene_item_transform",
            default=_MISSING,
        )
        mapping = _as_mapping(data)
        if mapping is None:
            raise TransformContractError(
                "GetSceneItemTransform did not return a sceneItemTransform object"
            )
        return dict(mapping)

    def _map_monitor_id(
        self,
        *,
        source_name: str,
        input_kind: str,
        monitor_id: str,
        settings: Any,
        displays: list[DisplayInfo],
        source_width: float,
        source_height: float,
    ) -> CaptureMapping:
        matches: list[DisplayInfo] = []
        for display in displays:
            identifiers = {display.id, *display.aliases}
            if monitor_id in identifiers and display not in matches:
                matches.append(display)

        context = (
            f"source={_safe_text(source_name)!r}, kind={_safe_text(input_kind)!r}, "
            f"monitor_id=<redacted>, settings={_settings_summary(settings)}"
        )
        available = _available_display_ids(displays)
        if not matches:
            raise RuntimeError(
                "Unable to map OBS capture monitor; "
                f"{context}. Available display IDs: {available}"
            )
        if len(matches) != 1:
            raise RuntimeError(
                "OBS capture monitor identity is ambiguous; "
                f"{context}. Available display IDs: {available}"
            )

        display = matches[0]
        scale_x = source_width / float(display.width_px)
        scale_y = source_height / float(display.height_px)
        _number(scale_x, "display-to-source scaleX", positive=True)
        _number(scale_y, "display-to-source scaleY", positive=True)
        return CaptureMapping(
            display=display,
            source_width=source_width,
            source_height=source_height,
            display_to_source_scale_x=scale_x,
            display_to_source_scale_y=scale_y,
        )

    def find_display_capture(
        self,
        scene: str,
        source: str,
        *,
        displays: list[DisplayInfo] | None = None,
        canvas: tuple[float, float] | None = None,
    ) -> SceneItem:
        """Select a supported capture source and resolve its monitor.

        ``displays`` and ``canvas`` are additive keyword arguments. Existing
        two-argument callers continue to receive a :class:`SceneItem`, while
        the runtime supplies both values to activate Windows identity and
        full-canvas validation.
        """

        scene_name = self._resolve_scene_name(scene)
        scene_items = self._scene_items(scene_name)
        candidates: list[tuple[Any, str, str]] = []
        for item in scene_items:
            source_name = _field(item, "sourceName", "source_name", default="?")
            input_kind = _normalise_kind(
                _field(item, "inputKind", "input_kind", default=_MISSING)
            )
            if input_kind in CAPTURE_INPUT_KINDS:
                candidates.append((item, str(source_name), input_kind))

        selected: tuple[Any, str, str] | None = None
        if source:
            exact_items = [
                (
                    item,
                    str(_field(item, "sourceName", "source_name", default="?")),
                    _normalise_kind(
                        _field(item, "inputKind", "input_kind", default=_MISSING)
                    ),
                )
                for item in scene_items
                if str(_field(item, "sourceName", "source_name", default="?")) == source
            ]
            if len(exact_items) != 1:
                available = ", ".join(
                    _safe_text(_field(item, "sourceName", "source_name", default="?"))
                    for item in scene_items
                ) or "<none>"
                raise RuntimeError(
                    f"Configured OBS source {_safe_text(source)!r} was not an exact, "
                    f"unique supported capture in scene {_safe_text(scene_name)!r}. "
                    f"Available sources: {available}"
                )
            candidate = exact_items[0]
            if candidate[2] not in CAPTURE_INPUT_KINDS:
                raise RuntimeError(
                    f"Configured OBS source {_safe_text(source)!r} has unsupported "
                    f"input kind {_safe_text(candidate[2] or '<missing>')!r}. "
                    f"Supported kinds: {', '.join(sorted(CAPTURE_INPUT_KINDS))}"
                )
            selected = candidate
        elif len(candidates) == 1:
            selected = candidates[0]
        elif not candidates:
            available = ", ".join(
                _safe_text(_field(item, "sourceName", "source_name", default="?"))
                for item in scene_items
            ) or "<none>"
            raise RuntimeError(
                f"No supported OBS capture source found in scene {_safe_text(scene_name)!r}. "
                f"Available sources: {available}"
            )
        else:
            names = ", ".join(_safe_text(candidate[1]) for candidate in candidates)
            raise RuntimeError(
                "Multiple supported OBS capture sources found in scene "
                f"{_safe_text(scene_name)!r}; set [obs].source exactly. "
                f"Candidates: {names}"
            )

        assert selected is not None
        item, source_name, input_kind = selected
        raw_item_id = _field(item, "sceneItemId", "scene_item_id", default=_MISSING)
        try:
            item_id = int(raw_item_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"OBS capture source {_safe_text(source_name)!r} has an invalid scene item ID"
            ) from exc

        transform_data = self._read_transform_data(scene_name, item_id)
        source_width = _number_from(
            transform_data,
            ("sourceWidth", "source_width"),
            "sourceWidth",
            positive=True,
        )
        source_height = _number_from(
            transform_data,
            ("sourceHeight", "source_height"),
            "sourceHeight",
            positive=True,
        )
        try:
            validate_raw_transform(
                transform_data,
                source_size=(source_width, source_height),
                canvas=canvas,
                strict=True,
            )
        except TransformContractError as exc:
            raise TransformContractError(
                f"OBS capture source {_safe_text(source_name)!r} "
                f"(kind={_safe_text(input_kind)!r}) failed the transform contract: "
                f"{_safe_text(exc)}"
            ) from exc

        monitor_id: str | None = None
        capture_mapping: CaptureMapping | None = None
        if input_kind == MONITOR_CAPTURE_KIND:
            settings_result = self._require_client().get_input_settings(source_name)
            settings = _field(
                settings_result,
                "inputSettings",
                "input_settings",
                default=_MISSING,
            )
            if settings is _MISSING:
                # Keep direct mapping fakes useful while still using the
                # actual obsws-python response shape in production.
                settings = settings_result if _as_mapping(settings_result) else {}
            monitor_value = _field(
                settings,
                "monitor_id",
                "monitorId",
                default=_MISSING,
            )
            if monitor_value is _MISSING or monitor_value in (None, ""):
                available = (
                    f" Available display IDs: {_available_display_ids(list(displays))}"
                    if displays is not None
                    else ""
                )
                raise RuntimeError(
                    "OBS monitor_capture source has no monitor_id; "
                    f"source={_safe_text(source_name)!r}, kind={_safe_text(input_kind)!r}, "
                    f"settings={_settings_summary(settings)}.{available}"
                )
            monitor_id = str(monitor_value)
            if displays is not None:
                capture_mapping = self._map_monitor_id(
                    source_name=source_name,
                    input_kind=input_kind,
                    monitor_id=monitor_id,
                    settings=settings,
                    displays=list(displays),
                    source_width=source_width,
                    source_height=source_height,
                )

        return SceneItem(
            scene_name=scene_name,
            item_id=item_id,
            source_width=source_width,
            source_height=source_height,
            source_name=source_name,
            input_kind=input_kind,
            monitor_id=monitor_id,
            capture_display=(capture_mapping.display if capture_mapping else None),
            display_to_source_scale_x=(
                capture_mapping.display_to_source_scale_x if capture_mapping else 1.0
            ),
            display_to_source_scale_y=(
                capture_mapping.display_to_source_scale_y if capture_mapping else 1.0
            ),
            capture_mapping=capture_mapping,
            _raw_transform=MappingProxyType(dict(transform_data)),
        )

    def get_transform(self, item: SceneItem) -> Transform:
        """Get and validate the current scene item transform."""

        transform_data = self._read_transform_data(item.scene_name, item.item_id)
        return validate_raw_transform(
            transform_data,
            source_size=(item.source_width, item.source_height),
            strict=True,
        )

    def validate_transform_contract(
        self, item: SceneItem, canvas: tuple[float, float]
    ) -> None:
        """Validate the selected source's full-canvas startup contract."""

        transform_data = self._read_transform_data(item.scene_name, item.item_id)
        validate_raw_transform(
            transform_data,
            source_size=(item.source_width, item.source_height),
            canvas=canvas,
            strict=True,
        )

    def set_transform(self, item: SceneItem, t: Transform) -> None:
        """Validate and set a scene item transform."""

        _validate_outgoing_transform(t)
        self._require_client().set_scene_item_transform(
            item.scene_name,
            item.item_id,
            {
                "positionX": t.pos_x,
                "positionY": t.pos_y,
                "scaleX": t.scale_x,
                "scaleY": t.scale_y,
            },
        )


__all__ = [
    "CAPTURE_INPUT_KINDS",
    "MONITOR_CAPTURE_KIND",
    "CaptureMapping",
    "ObsClient",
    "SceneItem",
    "TransformContractError",
    "validate_raw_transform",
]
