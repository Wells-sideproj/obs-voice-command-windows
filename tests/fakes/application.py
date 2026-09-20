"""Small deterministic ports for hardware-free application tests.

These fakes model the boundaries used by :mod:`obs_voice_command.runtime`.
They deliberately do not replace the matcher, controller, or transform math;
the tests inject only audio, pointer, and OBS side effects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import threading
from types import SimpleNamespace
from typing import Any, Callable, Iterable

import numpy as np

from obs_voice_command.obs_client import CaptureMapping
from obs_voice_command.platform.common import DisplayInfo, Point, locate_point
from obs_voice_command.zoom import Transform


@dataclass(frozen=True)
class AudioFrame:
    """One scripted audio result, or a final frame that raises Ctrl-C."""

    text: str = ""
    endpoint: bool = False
    stop: bool = False
    wait_before_emit: threading.Event | None = field(
        default=None, compare=False, repr=False
    )
    wait_after_emit: threading.Event | None = field(
        default=None, compare=False, repr=False
    )
    synchronization_timeout: float = field(default=2.0, compare=False, repr=False)


class FakeAsr:
    """Decode numeric test samples into a predeclared deterministic script."""

    def __init__(
        self,
        frames: Iterable[AudioFrame],
        *,
        on_frame: Callable[[int, AudioFrame], None] | None = None,
        on_stop: Callable[[], None] | None = None,
    ) -> None:
        self.frames = tuple(frames)
        self.on_frame = on_frame
        self.on_stop = on_stop
        self.calls: list[int] = []

    def feed(self, samples: np.ndarray) -> tuple[str, bool]:
        index = int(np.asarray(samples).reshape(-1)[0])
        if not 0 <= index < len(self.frames):
            raise AssertionError(f"unknown fake audio frame index: {index}")

        frame = self.frames[index]
        self.calls.append(index)
        if self.on_frame is not None:
            self.on_frame(index, frame)
        if frame.stop:
            if self.on_stop is not None:
                self.on_stop()
            raise KeyboardInterrupt()
        return frame.text, frame.endpoint


@dataclass(frozen=True)
class AudioQueueStep:
    """One item returned by :class:`FakeAudioQueue`."""

    index: int
    before_get: Callable[[], None] | None = field(default=None, compare=False, repr=False)
    wait_for: threading.Event | None = field(default=None, compare=False, repr=False)
    after_wait: Callable[[], None] | None = field(default=None, compare=False, repr=False)
    synchronization_timeout: float = field(default=2.0, compare=False, repr=False)


class DeterministicWait:
    """Permit-based controller wait with bounded watchdogs and error capture."""

    def __init__(self, synchronization_timeout: float = 2.0) -> None:
        self.synchronization_timeout = synchronization_timeout
        self.errors: list[BaseException] = []
        self._condition = threading.Condition()
        self._permits = 0
        self._completed = 0
        self._released = False

    @property
    def completed(self) -> int:
        with self._condition:
            return self._completed

    def __call__(self, seconds: float) -> None:
        del seconds
        with self._condition:
            self._completed += 1
            self._condition.notify_all()
            while self._permits == 0 and not self._released:
                if not self._condition.wait(self.synchronization_timeout):
                    error = AssertionError("timed out waiting for a controller tick permit")
                    self.errors.append(error)
                    raise error
            if self._permits:
                self._permits -= 1

    def release(self) -> None:
        with self._condition:
            self._permits += 1
            self._condition.notify_all()

    def release_all(self) -> None:
        """Unblock every pending wait during test cleanup."""

        with self._condition:
            self._released = True
            self._condition.notify_all()

    def wait_for_completion(self, previous: int) -> None:
        """Wait for a tick after ``previous``; timeout is only a watchdog."""

        with self._condition:
            while self._completed <= previous:
                if not self._condition.wait(self.synchronization_timeout):
                    error = AssertionError("timed out waiting for controller tick completion")
                    self.errors.append(error)
                    raise error

    def release_until(self, event: threading.Event, max_ticks: int = 128) -> None:
        """Drive one continuous controller until ``event`` is observed.

        Each permit advances exactly one controller loop.  The finite bound is
        a watchdog for a broken state transition, not a timing assumption.
        """

        for _ in range(max_ticks):
            if event.is_set():
                return
            previous = self.completed
            self.release()
            self.wait_for_completion(previous)
        if not event.is_set():
            error = AssertionError("controller did not reach the expected fake-OBS state")
            self.errors.append(error)
            raise error


class FakeAudioQueue:
    """Queue-like scripted audio source for one continuous Runtime audio loop."""

    def __init__(self, steps: Iterable[AudioQueueStep]) -> None:
        self.steps = list(steps)
        self.calls = 0

    def get(self, timeout: float | None = None) -> np.ndarray:
        del timeout
        if not self.steps:
            raise AssertionError("runtime requested audio after the stop frame")

        step = self.steps.pop(0)
        self.calls += 1
        if step.before_get is not None:
            step.before_get()
        if step.wait_for is not None and not step.wait_for.wait(
            step.synchronization_timeout
        ):
            raise AssertionError(f"timed out waiting for audio step {step.index}")
        if step.after_wait is not None:
            step.after_wait()
        return np.asarray([float(step.index)], dtype=np.float32)


class FakeAudioStream:
    """Emit scripted callback frames from a producer thread without sleeping."""

    def __init__(
        self,
        frames: Iterable[AudioFrame],
        callback: Callable[..., Any],
        *,
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        self.frames = tuple(frames)
        self.callback = callback
        self.on_closed = on_closed
        self.entered = threading.Event()
        self.finished = threading.Event()
        self.closed = threading.Event()
        self.emitted: list[int] = []
        self.errors: list[BaseException] = []
        self._producer: threading.Thread | None = None
        self._last_emitted = -1
        self._stop_index = next(
            (index for index in range(len(self.frames) - 1, -1, -1) if self.frames[index].stop),
            None,
        )

    def __enter__(self) -> "FakeAudioStream":
        self.entered.set()
        self._producer = threading.Thread(
            target=self._emit, name="fake-audio-producer", daemon=True
        )
        self._producer.start()
        return self

    def _wait_for(self, event: threading.Event, timeout: float, description: str) -> None:
        if not event.wait(timeout):
            raise AssertionError(f"timed out waiting for fake-audio {description}")

    def _emit(self) -> None:
        try:
            for index, frame in enumerate(self.frames):
                if frame.wait_before_emit is not None:
                    self._wait_for(
                        frame.wait_before_emit,
                        frame.synchronization_timeout,
                        f"before frame {index}",
                    )
                if self.closed.is_set():
                    return

                self.callback(
                    np.asarray([[float(index)]], dtype=np.float32),
                    1,
                    None,
                    None,
                )
                self._last_emitted = index
                self.emitted.append(index)

                if frame.wait_after_emit is not None:
                    self._wait_for(
                        frame.wait_after_emit,
                        frame.synchronization_timeout,
                        f"after frame {index}",
                    )
        except BaseException as exc:
            self.errors.append(exc)
            # A failed producer must still unblock Runtime._audio_loop so the
            # test reports the producer assertion instead of hanging forever.
            if self._stop_index is not None and self._last_emitted != self._stop_index:
                self.callback(
                    np.asarray([[float(self._stop_index)]], dtype=np.float32),
                    1,
                    None,
                    None,
                )
                self._last_emitted = self._stop_index
                self.emitted.append(self._stop_index)
        finally:
            self.finished.set()

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        self.closed.set()
        try:
            if self.on_closed is not None:
                self.on_closed()
        except BaseException as exc:
            self.errors.append(exc)
        if self._producer is not None and not self.finished.wait(2.0):
            self.errors.append(AssertionError("fake audio producer did not finish"))
        if self.errors:
            raise self.errors[0]
        return False


class DeterministicClock:
    """Return a repeatable sequence of timestamps for matcher/controller calls."""

    def __init__(self, start: float = 0.0, step: float = 0.0) -> None:
        self._value = start
        self._step = step
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            value = self._value
            self._value += self._step
            return value


class FakePointer:
    """Mutable pointer backed by the production display geometry locator."""

    def __init__(self, displays: Iterable[DisplayInfo], position: Point) -> None:
        self.displays = list(displays)
        self._position = position
        self._lock = threading.Lock()
        self.locate_calls: list[tuple[Point, tuple[DisplayInfo, ...]]] = []
        self.on_locate: Callable[[], None] | None = None

    def set_position(self, position: Point) -> None:
        with self._lock:
            self._position = position

    def get_displays(self) -> list[DisplayInfo]:
        return list(self.displays)

    def get_cursor_position(self) -> Point:
        with self._lock:
            return self._position

    def locate(
        self, position: Point, displays: list[DisplayInfo]
    ) -> tuple[DisplayInfo, float, float] | None:
        self.locate_calls.append((position, tuple(displays)))
        if self.on_locate is not None:
            self.on_locate()
        return locate_point(position, displays)


class FakeObs:
    """In-memory OBS port with transform ordering and reconnect assertions."""

    def __init__(
        self,
        *,
        item: Any,
        original: Transform,
        canvas: tuple[float, float],
        fail_first_non_original: bool = False,
        fail_reconnect_attempts: int = 0,
        fail_first_reconnect_baseline: bool = False,
    ) -> None:
        self.item = item
        self.original = original
        self.canvas = canvas
        self.fail_first_non_original = fail_first_non_original
        self.fail_reconnect_attempts = fail_reconnect_attempts
        self.fail_first_reconnect_baseline = fail_first_reconnect_baseline
        self.connected = False
        self.closed = False
        self.connect_attempts = 0
        self.connections = 0
        self.events: list[tuple[str, Any]] = []
        self.errors: list[BaseException] = []
        self.find_calls: list[tuple[str, str, tuple[DisplayInfo, ...], tuple[float, float]]] = []
        self.validation_calls: list[tuple[Any, tuple[float, float]]] = []
        self.successful_transforms: list[Transform] = []
        self.non_original_written = threading.Event()
        self.original_written = threading.Event()
        self.on_set_transform: Callable[[Transform], None] | None = None
        self._lock = threading.Lock()

    def connect(self) -> None:
        with self._lock:
            self.connect_attempts += 1
            if self.connections and self.fail_reconnect_attempts:
                self.fail_reconnect_attempts -= 1
                self.events.append(("connect_failed", None))
                raise ConnectionError("fake OBS reconnect failed")
            self.connected = True
            self.connections += 1
            self.events.append(("connect", None))

    def find_display_capture(
        self,
        scene: str,
        source: str,
        *,
        displays: list[DisplayInfo],
        canvas: tuple[float, float],
    ) -> Any:
        with self._lock:
            self.find_calls.append((scene, source, tuple(displays), canvas))
            self.events.append(("find_display_capture", (scene, source)))
        return self.item

    def get_canvas_size(self) -> tuple[float, float]:
        with self._lock:
            self.events.append(("get_canvas_size", self.canvas))
        return self.canvas

    def get_transform(self, item: Any) -> Transform:
        if item is not self.item:
            raise AssertionError("fake OBS received an unknown scene item")
        with self._lock:
            self.events.append(("get_transform", None))
        return self.original

    def validate_transform_contract(self, item: Any, canvas: tuple[float, float]) -> None:
        if item is not self.item:
            raise AssertionError("fake OBS received an unknown scene item")
        with self._lock:
            self.validation_calls.append((item, canvas))
            self.events.append(("validate_transform_contract", canvas))

    def set_transform(self, item: Any, transform: Transform) -> None:
        try:
            if item is not self.item:
                raise AssertionError("fake OBS received an unknown scene item")

            callback: Callable[[Transform], None] | None
            with self._lock:
                self.events.append(("set_attempt", transform))
                if not self.connected:
                    raise ConnectionError("fake OBS transport is disconnected")
                if transform != self.original and self.fail_first_non_original:
                    self.fail_first_non_original = False
                    self.connected = False
                    self.events.append(("disconnect", transform))
                    raise ConnectionError("fake OBS transport disconnected during write")
                if transform == self.original and self.fail_first_reconnect_baseline:
                    self.fail_first_reconnect_baseline = False
                    self.connected = False
                    self.events.append(("baseline_failed", transform))
                    raise ConnectionError("fake OBS baseline restore failed")

                self.successful_transforms.append(transform)
                self.events.append(("set", transform))
                if transform == self.original:
                    self.original_written.set()
                else:
                    self.non_original_written.set()
                callback = self.on_set_transform

            if callback is not None:
                callback(transform)
        except ConnectionError:
            raise
        except BaseException as exc:
            self.errors.append(exc)
            raise

    def close(self) -> None:
        with self._lock:
            self.connected = False
            self.closed = True
            self.events.append(("close", None))


def make_capture_item(
    display: DisplayInfo,
    *,
    source_size: tuple[float, float] | None = None,
    input_kind: str = "monitor_capture",
    display_to_source_scale: tuple[float, float] = (1.0, 1.0),
) -> Any:
    """Build the scene-item attributes Runtime reads from an OBS response."""

    source_width, source_height = source_size or (
        float(display.width_px) * display_to_source_scale[0],
        float(display.height_px) * display_to_source_scale[1],
    )
    mapping = None
    if input_kind == "monitor_capture":
        mapping = CaptureMapping(
            display=display,
            source_width=source_width,
            source_height=source_height,
            display_to_source_scale_x=display_to_source_scale[0],
            display_to_source_scale_y=display_to_source_scale[1],
        )
    return SimpleNamespace(
        source_width=source_width,
        source_height=source_height,
        input_kind=input_kind,
        capture_mapping=mapping,
    )


def display(
    *,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
    width_px: int = 1920,
    height_px: int = 1080,
    identifier: str = "fake-display",
    aliases: tuple[str, ...] = (),
) -> DisplayInfo:
    """Create a display whose pointer coordinate space is physical pixels."""

    return DisplayInfo(
        origin_x=origin_x,
        origin_y=origin_y,
        width_pts=float(width_px),
        height_pts=float(height_px),
        width_px=width_px,
        height_px=height_px,
        id=identifier,
        aliases=aliases,
    )


__all__ = [
    "AudioFrame",
    "AudioQueueStep",
    "DeterministicClock",
    "DeterministicWait",
    "FakeAsr",
    "FakeAudioQueue",
    "FakeAudioStream",
    "FakeObs",
    "FakePointer",
    "display",
    "make_capture_item",
]
