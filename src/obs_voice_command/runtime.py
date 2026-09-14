"""Injectable application runtime and zoom-controller lifecycle.

The command-line module is intentionally a small composition root. This
module owns the application lifecycle while keeping pointer, OBS, audio, ASR,
clock, and waiting behavior replaceable in hardware-free tests.
"""
from __future__ import annotations

import queue
import inspect
import math
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, TypeAlias

import numpy as np

from .config import AudioConfig, ObsConfig, load_config
from .matcher import Matcher
from .platform.common import DisplayInfo, Point, locate_point
from .zoom import Transform, apply_deadzone, compute_transform, smooth


class PointerPort(Protocol):
    """Pointer/display operations needed by the controller."""

    def get_displays(self) -> list[DisplayInfo]:
        """Return displays in the pointer backend's coordinate space."""

    def get_cursor_position(self) -> Point:
        """Return the current cursor position."""

    def locate(
        self, position: Point, displays: list[DisplayInfo]
    ) -> tuple[DisplayInfo, float, float] | None:
        """Map a cursor position to display-relative source pixels."""


class ObsPort(Protocol):
    """Subset of the OBS client used by the runtime."""

    def connect(self) -> None:
        """Connect to OBS."""

    def find_display_capture(
        self,
        scene: str,
        source: str,
        *,
        displays: list[DisplayInfo] | None = None,
        canvas: tuple[float, float] | None = None,
    ) -> Any:
        """Resolve the configured OBS scene item."""

    def validate_transform_contract(
        self, item: Any, canvas: tuple[float, float]
    ) -> None:
        """Validate the selected source before audio or transform writes."""

    def get_transform(self, item: Any) -> Transform:
        """Read a scene-item transform."""

    def get_canvas_size(self) -> tuple[float, float]:
        """Read the OBS canvas dimensions."""

    def set_transform(self, item: Any, transform: Transform) -> None:
        """Write a scene-item transform."""


class AsrPort(Protocol):
    """Streaming ASR boundary."""

    def feed(self, samples: np.ndarray) -> tuple[str, bool]:
        """Feed one audio chunk and return text plus endpoint state."""


class AudioStreamPort(Protocol):
    """Context-managed audio stream boundary."""

    def __enter__(self) -> "AudioStreamPort":
        """Start the stream."""

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Any:
        """Stop the stream."""


AudioCallback: TypeAlias = Callable[[Any, int, Any, Any], None]
Clock: TypeAlias = Callable[[], float]
Wait: TypeAlias = Callable[[float], None]


class RuntimeStartupError(RuntimeError):
    """A user-actionable failure while composing the runtime."""


def _display_tokens(display: Any) -> set[str]:
    identifiers = [
        getattr(display, "id", ""),
        *tuple(getattr(display, "aliases", ()) or ()),
    ]
    return {str(identifier) for identifier in identifiers if identifier}


def _same_display(left: Any, right: Any) -> bool:
    """Match the stable display identity and its geometry, never dimensions alone."""

    if left is right:
        return True
    if not _display_tokens(left).intersection(_display_tokens(right)):
        return False
    for name in (
        "origin_x",
        "origin_y",
        "width_pts",
        "height_pts",
        "width_px",
        "height_px",
    ):
        try:
            if not math.isclose(float(getattr(left, name)), float(getattr(right, name))):
                return False
        except (AttributeError, TypeError, ValueError):
            return False
    return True


def _find_capture_item(
    obs: ObsPort,
    scene: str,
    source: str,
    displays: list[DisplayInfo],
    canvas: tuple[float, float],
) -> Any:
    """Call the additive capture contract while retaining old fake compatibility."""

    finder = obs.find_display_capture
    try:
        parameters = inspect.signature(finder).parameters.values()
    except (TypeError, ValueError):
        accepts_kwargs = True
        names: set[str] = set()
    else:
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        names = {
            parameter.name
            for parameter in inspect.signature(finder).parameters.values()
        }

    kwargs: dict[str, Any] = {}
    if accepts_kwargs or "displays" in names:
        kwargs["displays"] = displays
    if accepts_kwargs or "canvas" in names:
        kwargs["canvas"] = canvas
    return finder(scene, source, **kwargs)


def _capture_controller_values(
    item: Any,
    displays: list[DisplayInfo],
    source: tuple[float, float],
) -> tuple[list[DisplayInfo], DisplayInfo | None, tuple[float, float] | None]:
    """Resolve the selected OBS display and display-pixel scaling for the controller."""

    mapping = getattr(item, "capture_mapping", None)
    capture_display = getattr(mapping, "display", None) if mapping is not None else None
    if capture_display is None:
        capture_display = getattr(item, "capture_display", None)

    input_kind = str(getattr(item, "input_kind", "") or "").strip().lower()
    if input_kind == "monitor_capture" and capture_display is None:
        raise RuntimeStartupError(
            "OBS monitor_capture was selected but its monitor identity was not mapped"
        )
    if capture_display is None:
        return displays, None, None

    matched_display = next(
        (display for display in displays if _same_display(capture_display, display)),
        None,
    )
    if matched_display is None:
        raise RuntimeStartupError(
            "OBS capture display is not present in the pointer display inventory"
        )

    if mapping is not None:
        scale = (
            float(mapping.display_to_source_scale_x),
            float(mapping.display_to_source_scale_y),
        )
    else:
        # A display capture without a monitor_id (the macOS-compatible path)
        # still needs the physical-display-pixel to source-pixel conversion.
        scale = (
            float(source[0]) / float(matched_display.width_px),
            float(source[1]) / float(matched_display.height_px),
        )
    if any(not math.isfinite(value) or value <= 0 for value in scale):
        raise RuntimeStartupError("OBS capture display-to-source scaling is invalid")
    return [matched_display], matched_display, scale


class SystemPointer:
    """Lazy adapter around the existing platform-neutral mouse facade."""

    def get_displays(self) -> list[DisplayInfo]:
        from .mouse import get_displays

        return get_displays()

    def get_cursor_position(self) -> Point:
        from .mouse import get_mouse_pos

        return get_mouse_pos()

    def locate(
        self, position: Point, displays: list[DisplayInfo]
    ) -> tuple[DisplayInfo, float, float] | None:
        from .mouse import locate

        return locate(position, displays)


def default_pointer_factory() -> PointerPort:
    """Build the real pointer adapter only when normal mode needs it."""
    return SystemPointer()


def default_model_factory() -> Path:
    """Load the model lazily so capability-only commands do not download it."""
    from .asr import ensure_model

    return ensure_model()


def default_asr_factory(model_dir: Path) -> AsrPort:
    """Build ASR only after the model/capability guards have passed."""
    from .asr import Asr

    return Asr(model_dir)


def default_obs_factory(obs_config: ObsConfig) -> ObsPort:
    """Build the OBS client without connecting; connect is lifecycle-owned."""
    from .obs_client import ObsClient

    return ObsClient(obs_config.host, obs_config.port, obs_config.password)


def default_audio_factory(
    audio_config: AudioConfig, callback: AudioCallback
) -> AudioStreamPort:
    """Build the real input stream only after all earlier startup succeeds."""
    import sounddevice

    try:
        return sounddevice.InputStream(
            samplerate=16000,
            channels=1,
            dtype="float32",
            blocksize=1600,
            device=audio_config.device or None,
            callback=callback,
        )
    except sounddevice.PortAudioError as exc:
        raise RuntimeStartupError(
            "麥克風開啟失敗 — 檢查「系統設定 → 隱私權與安全性 → 麥克風」"
            f"權限。錯誤: {exc}"
        ) from exc


def default_device_lister() -> Any:
    """Query devices lazily for --list-devices."""
    import sounddevice

    return sounddevice.query_devices()


@dataclass(frozen=True)
class RuntimeDependencies:
    """Factories and timing hooks used by :class:`Runtime`."""

    pointer_factory: Callable[[], PointerPort] = default_pointer_factory
    obs_factory: Callable[[ObsConfig], ObsPort] = default_obs_factory
    audio_factory: Callable[[AudioConfig, AudioCallback], AudioStreamPort] = (
        default_audio_factory
    )
    asr_factory: Callable[[Path], AsrPort] = default_asr_factory
    model_factory: Callable[[], Path] = default_model_factory
    device_lister: Callable[[], Any] = default_device_lister
    clock: Clock = time.monotonic
    wait: Wait | None = None


class ZoomController(threading.Thread):
    """Run the zoom loop with injectable pointer and lifecycle dependencies."""

    def __init__(
        self,
        obs: ObsPort | None,
        item: Any,
        orig: Transform,
        canvas: tuple[float, float],
        src: tuple[float, float],
        zoom_cfg: Any,
        displays: list[DisplayInfo],
        dry_run: bool,
        *,
        pointer: PointerPort | None = None,
        clock: Clock | None = None,
        wait: Wait | None = None,
        reconnect_delay: float = 3.0,
        capture_display: DisplayInfo | None = None,
        display_to_source_scale: tuple[float, float] = (1.0, 1.0),
    ) -> None:
        super().__init__(daemon=True, name="obs-voice-zoom-controller")
        self._obs = obs
        self._item = item
        self._orig = orig
        self._canvas = canvas
        self._src = src
        self._zoom_cfg = zoom_cfg
        self._displays = list(displays)
        self._capture_display = capture_display
        if self._capture_display is None and len(self._displays) == 1:
            self._capture_display = self._displays[0]

        try:
            self._display_to_source_scale = (
                float(display_to_source_scale[0]),
                float(display_to_source_scale[1]),
            )
        except (IndexError, TypeError, ValueError) as exc:
            raise ValueError("display-to-source scaling must contain two numbers") from exc
        if any(
            not math.isfinite(value) or value <= 0
            for value in self._display_to_source_scale
        ):
            raise ValueError("display-to-source scaling must be positive and finite")
        self._dry_run = dry_run
        self._pointer = pointer or SystemPointer()
        self._clock = clock or time.monotonic
        self._reconnect_delay = reconnect_delay

        self._target_z = 1.0
        self._cur_z = 1.0
        self._cur_cx = src[0] / 2.0
        self._cur_cy = src[1] / 2.0
        self._target_cx = self._cur_cx
        self._target_cy = self._cur_cy

        self._state_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._idle = True
        self._stopping = False
        self._restored = False
        self._thread_started = False
        self._wait = wait or self._stop_event.wait

    def start(self) -> None:
        """Track whether the thread started so partial startup can be cleaned."""
        self._thread_started = True
        try:
            super().start()
        except BaseException:
            self._thread_started = False
            raise

    def _is_stopping(self) -> bool:
        with self._state_lock:
            return self._stopping

    def handle(self, action: str) -> None:
        """Handle zoom_in / zoom_out actions from the audio loop."""
        with self._state_lock:
            if self._stopping:
                return

            if action == "zoom_in":
                if self._target_z == 1.0:
                    mouse = self._mouse_src_px()
                    if mouse:
                        self._target_cx, self._target_cy = mouse
                        self._cur_cx, self._cur_cy = mouse
                    self._target_z = self._zoom_cfg.level
                    print("[ZOOM] in")
                else:
                    print("[ZOOM] already in that state, ignored")

            elif action == "zoom_out":
                if self._target_z > 1.0:
                    self._target_z = 1.0
                    print("[ZOOM] out")
                else:
                    print("[ZOOM] already in that state, ignored")

    def _mouse_src_px(self) -> tuple[float, float] | None:
        """Get current mouse position in source pixels when available."""
        try:
            get_position = getattr(self._pointer, "get_cursor_position", None)
            if get_position is None:
                get_position = getattr(self._pointer, "get_mouse_pos")
            mouse_pos = get_position()

            locate = getattr(self._pointer, "locate", None)
            result = (
                locate(mouse_pos, self._displays)
                if locate is not None
                else locate_point(mouse_pos, self._displays)
            )
            if result:
                located_display, px, py = result
                if self._capture_display is not None and not _same_display(
                    located_display, self._capture_display
                ):
                    return None

                contains = getattr(located_display, "contains", None)
                if callable(contains) and not contains(mouse_pos):
                    return None

                px = float(px)
                py = float(py)
                if (
                    not math.isfinite(px)
                    or not math.isfinite(py)
                    or px < 0
                    or py < 0
                    or px >= float(located_display.width_px)
                    or py >= float(located_display.height_px)
                ):
                    return None
                return (
                    px * self._display_to_source_scale[0],
                    py * self._display_to_source_scale[1],
                )
        except Exception:
            # A transient pointer/backend failure must not kill the zoom loop.
            pass
        return None

    def run(self) -> None:
        """Main loop: tick at 30fps and apply transforms."""
        frame_time = 1.0 / 30.0

        try:
            while not self._stop_event.is_set():
                start = self._clock()

                with self._state_lock:
                    if self._stopping:
                        break

                    # Keep validating capture membership while zooming out:
                    # the level animation can outlive the zoom-in target
                    # center smoothing.
                    if self._target_z > 1.0 or self._cur_z > 1.0:
                        mouse = self._mouse_src_px()
                        if mouse is not None:
                            radius = (
                                self._src[0] / self._target_z
                            ) * self._zoom_cfg.deadzone
                            self._target_cx, self._target_cy = apply_deadzone(
                                self._target_cx,
                                self._target_cy,
                                mouse[0],
                                mouse[1],
                                radius,
                            )
                        else:
                            # Do not let a stale target continue moving the
                            # center while the cursor is outside the selected
                            # capture display.  Re-entry can establish a new
                            # target on the next tick.
                            self._target_cx = self._cur_cx
                            self._target_cy = self._cur_cy

                    self._cur_z = smooth(
                        self._cur_z, self._target_z, self._zoom_cfg.smoothing
                    )
                    self._cur_cx = smooth(
                        self._cur_cx, self._target_cx, self._zoom_cfg.smoothing
                    )
                    self._cur_cy = smooth(
                        self._cur_cy, self._target_cy, self._zoom_cfg.smoothing
                    )

                    transform = compute_transform(
                        self._orig,
                        self._canvas,
                        self._src,
                        self._cur_z,
                        self._cur_cx,
                        self._cur_cy,
                    )

                    if self._cur_z == 1.0 and self._target_z == 1.0:
                        to_send = None if self._idle else self._orig
                        self._idle = True
                    else:
                        self._idle = False
                        to_send = transform

                if to_send is not None:
                    self._send_transform(to_send)

                elapsed = self._clock() - start
                self._wait(max(0.0, frame_time - elapsed))
        except Exception as exc:
            print(f"[ZOOM] controller thread died: {exc!r}")

    def _send_transform(self, transform: Transform) -> None:
        """Send a transform, reconnecting through the same serialized path."""
        if self._dry_run:
            if not self._is_stopping():
                print(
                    f"[TRANSFORM] x={transform.pos_x:.1f} y={transform.pos_y:.1f} "
                    f"sx={transform.scale_x:.2f} sy={transform.scale_y:.2f}"
                )
            return

        with self._send_lock:
            if self._is_stopping():
                return
            try:
                self._obs.set_transform(self._item, transform)  # type: ignore[union-attr]
            except Exception as exc:
                print(f"[ERROR] Failed to set transform: {exc}")
                self._reconnect()

    def _reconnect(self) -> None:
        """Reconnect, restore baseline, then reset controller state."""
        while not self._stop_event.is_set():
            self._wait(self._reconnect_delay)
            if self._is_stopping():
                return

            try:
                self._obs.connect()  # type: ignore[union-attr]
                if self._is_stopping():
                    return

                # The baseline write is serialized with every other write.
                # stop() waits for this thread before its final restore, so a
                # late zoom transform cannot follow that final baseline write.
                self._obs.set_transform(self._item, self._orig)  # type: ignore[union-attr]
                self._reset_state()
                print("[RECONNECT] Restored to original transform")
                return
            except Exception as reconnect_error:
                print(f"[RECONNECT] Still failing: {reconnect_error}")

    def _reset_state(self) -> None:
        with self._state_lock:
            self._target_z = 1.0
            self._cur_z = 1.0
            self._cur_cx = self._src[0] / 2.0
            self._cur_cy = self._src[1] / 2.0
            self._target_cx = self._cur_cx
            self._target_cy = self._cur_cy
            self._idle = True

    def stop(self) -> None:
        """Stop and restore baseline after the worker can no longer send."""
        with self._state_lock:
            self._stopping = True
            self._stop_event.set()

        # The default wait is Event.wait, which is interrupted by stop().
        # Waiting for the thread (rather than timing out) is what makes the
        # final restore ordered after every possible late transform.
        if self._thread_started and threading.current_thread() is not self:
            self.join()

        if self._dry_run or self._obs is None or self._item is None:
            return

        with self._send_lock:
            if self._restored:
                return
            try:
                self._obs.set_transform(self._item, self._orig)
            except Exception:
                # Preserve the existing best-effort shutdown behavior.
                pass
            finally:
                self._restored = True


def _close_if_possible(resource: Any) -> None:
    close = getattr(resource, "close", None)
    if close is not None:
        close()


def _install_sigterm_handler() -> Any:
    previous = signal.getsignal(signal.SIGTERM)

    def handler(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, handler)
    return previous


def _restore_sigterm_handler(previous: Any) -> None:
    signal.signal(signal.SIGTERM, previous)


class Runtime:
    """Compose the real application or a fully fake test runtime."""

    def __init__(self, args: Any, dependencies: RuntimeDependencies | None = None):
        self.args = args
        self.dependencies = dependencies or RuntimeDependencies()

    def run(self) -> None:
        """Run until Ctrl-C, SIGTERM, or an injected stream ends the loop."""
        args = self.args
        dependencies = self.dependencies

        if getattr(args, "list_devices", False):
            print(dependencies.device_lister())
            return

        # This is deliberately the first capability check. On Windows it
        # exits before model download, ASR construction, microphone creation,
        # pointer access, or OBS construction/connection.
        if getattr(args, "os", False):
            from . import os_zoom

            os_zoom.ensure_supported()

        config = load_config(Path(args.config))
        controller: ZoomController | None = None
        stream: AudioStreamPort | None = None
        stream_entered = False
        obs: ObsPort | None = None
        signal_previous: Any = None
        signal_installed = False

        try:
            print("Loading ASR model...")
            model_dir = dependencies.model_factory()
            asr = dependencies.asr_factory(model_dir)

            pointer: PointerPort | None = None
            displays: list[DisplayInfo] = []
            if not getattr(args, "os", False):
                pointer = dependencies.pointer_factory()
                displays = pointer.get_displays()

            if not getattr(args, "dry_run", False) and not getattr(args, "os", False):
                obs = dependencies.obs_factory(config.obs)
                try:
                    obs.connect()
                except ConnectionError as exc:
                    raise RuntimeStartupError(str(exc)) from exc

                try:
                    canvas = obs.get_canvas_size()
                    item = _find_capture_item(
                        obs,
                        config.obs.scene,
                        config.obs.source,
                        displays,
                        canvas,
                    )
                    original = obs.get_transform(item)
                    source = (item.source_width, item.source_height)
                    validator = getattr(obs, "validate_transform_contract", None)
                    if callable(validator):
                        validator(item, canvas)
                    controller_displays, capture_display, display_to_source_scale = (
                        _capture_controller_values(item, displays, source)
                    )
                except RuntimeStartupError:
                    raise
                except Exception as exc:
                    raise RuntimeStartupError(str(exc)) from exc
            else:
                item = None
                original = Transform(0, 0, 1, 1)
                canvas = (1920, 1080)
                if displays:
                    first = displays[0]
                    source = (first.width_px, first.height_px)
                else:
                    source = (1920, 1080)
                controller_displays = displays
                capture_display = None
                display_to_source_scale = (1.0, 1.0)

            if not getattr(args, "os", False):
                controller = ZoomController(
                    obs,
                    item,
                    original,
                    canvas,
                    source,
                    config.zoom,
                    controller_displays,
                    bool(getattr(args, "dry_run", False)),
                    pointer=pointer,
                    clock=dependencies.clock,
                    wait=dependencies.wait,
                    capture_display=capture_display,
                    display_to_source_scale=display_to_source_scale
                    or (1.0, 1.0),
                )

            matcher = Matcher(config.commands)
            audio_queue: queue.Queue[np.ndarray] = queue.Queue()

            def audio_callback(
                indata: Any, frames: int, time_info: Any, status: Any
            ) -> None:
                del frames, time_info
                if status:
                    print(f"[AUDIO] {status}")
                samples = np.asarray(indata)
                audio_queue.put(samples[:, 0].copy())

            stream = dependencies.audio_factory(config.audio, audio_callback)
            signal_previous = _install_sigterm_handler()
            signal_installed = True

            try:
                with stream:
                    stream_entered = True
                    if controller is not None:
                        controller.start()
                    self._audio_loop(
                        args, config, asr, matcher, controller, audio_queue
                    )
            except KeyboardInterrupt:
                # Normal Ctrl-C follows the same cleanup path as a clean stop.
                pass
        finally:
            if controller is not None:
                controller.stop()
            if signal_installed:
                _restore_sigterm_handler(signal_previous)
            if stream is not None and not stream_entered:
                _close_if_possible(stream)
            if obs is not None:
                _close_if_possible(obs)
            print("bye")

    def _audio_loop(
        self,
        args: Any,
        config: Any,
        asr: AsrPort,
        matcher: Matcher,
        controller: ZoomController | None,
        audio_queue: queue.Queue[np.ndarray],
    ) -> None:
        last_text = ""
        while True:
            try:
                chunk = audio_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            text, endpoint = asr.feed(chunk)
            if text != last_text:
                print(f"\r[ASR] {text}", end="", flush=True)
                last_text = text

            action = matcher.feed(text, now=self.dependencies.clock())
            if action:
                print()
                print(f"[TRIGGER] {action}")
                if getattr(args, "os", False):
                    from . import os_zoom

                    if getattr(args, "dry_run", False):
                        print(f"[OS-ZOOM] {action} (dry-run, 不送出按鍵)")
                    elif action == "zoom_in":
                        os_zoom.zoom_in(target=config.zoom.os_level)
                        print(f"[OS-ZOOM] in → {config.zoom.os_level}x")
                    else:
                        os_zoom.zoom_out()
                        print("[OS-ZOOM] out")
                elif controller is not None:
                    controller.handle(action)

            if endpoint:
                matcher.reset_utterance()
                if text:
                    print()


def run(args: Any, dependencies: RuntimeDependencies | None = None) -> None:
    """Convenience entry point for callers that need an injected runtime."""
    Runtime(args, dependencies).run()


__all__ = [
    "AsrPort",
    "AudioStreamPort",
    "ObsPort",
    "PointerPort",
    "Runtime",
    "RuntimeDependencies",
    "RuntimeStartupError",
    "SystemPointer",
    "ZoomController",
    "run",
]
