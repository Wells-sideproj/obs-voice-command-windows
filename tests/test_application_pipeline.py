from __future__ import annotations

from argparse import Namespace
from collections.abc import Callable, Iterable
from pathlib import Path

from obs_voice_command.main import main
from obs_voice_command.config import ZoomConfig, load_config
from obs_voice_command.matcher import Matcher
from obs_voice_command.platform.common import DisplayInfo
from obs_voice_command.runtime import Runtime, RuntimeDependencies, ZoomController
from obs_voice_command.zoom import Transform, compute_transform
from tests.fakes.application import (
    AudioFrame,
    AudioQueueStep,
    DeterministicClock,
    DeterministicWait,
    FakeAsr,
    FakeAudioStream,
    FakeAudioQueue,
    FakeObs,
    FakePointer,
    display,
    make_capture_item,
)


CANVAS = (1920.0, 1080.0)
SOURCE = (1920.0, 1080.0)
ORIGINAL = Transform(pos_x=7.0, pos_y=11.0, scale_x=1.0, scale_y=1.0)
CAPTURE_DISPLAY = display(identifier="capture-display")


def _runtime_args() -> Namespace:
    return Namespace(
        config="w11-007-missing-config.toml",
        dry_run=False,
        os=False,
        list_devices=False,
    )


def test_runtime_run_ctrl_c_restores_original_through_production_cleanup() -> None:
    """Exercise the real composition root and let Runtime own Ctrl-C cleanup."""

    item = make_capture_item(CAPTURE_DISPLAY, source_size=SOURCE)
    obs = FakeObs(item=item, original=ORIGINAL, canvas=CANVAS)
    waiter = DeterministicWait()
    clock = DeterministicClock()
    pointer = FakePointer([CAPTURE_DISPLAY], position=(1440.0, 540.0))
    release_action_tick = True

    def release_controller_after_zoom_action() -> None:
        nonlocal release_action_tick
        if release_action_tick:
            release_action_tick = False
            waiter.release()

    pointer.on_locate = release_controller_after_zoom_action
    frames = (
        AudioFrame("來個特寫", wait_after_emit=obs.non_original_written),
        AudioFrame(stop=True),
    )
    stop_observation: list[bool] = []
    asr = FakeAsr(
        frames,
        on_stop=lambda: stop_observation.append(obs.original_written.is_set()),
    )
    stream_holder: dict[str, FakeAudioStream] = {}

    def audio_factory(config, callback):
        del config
        stream = FakeAudioStream(
            frames,
            callback,
            on_closed=waiter.release_all,
        )
        stream_holder["stream"] = stream
        return stream

    dependencies = RuntimeDependencies(
        model_factory=lambda: Path("fake-model"),
        asr_factory=lambda model_dir: asr,
        pointer_factory=lambda: pointer,
        obs_factory=lambda config: obs,
        audio_factory=audio_factory,
        clock=clock,
        wait=waiter,
    )

    try:
        main(_runtime_args(), dependencies=dependencies)
    finally:
        # The production finally owns controller.stop(); this only prevents a
        # failed assertion or startup path from leaving an injected wait held.
        waiter.release_all()

    stream = stream_holder["stream"]
    assert stream.entered.is_set()
    assert stream.finished.is_set()
    assert stream.errors == []
    assert asr.calls == [0, 1]
    assert stop_observation == [False]
    assert waiter.errors == []
    assert any(transform != ORIGINAL for transform in obs.successful_transforms)
    assert obs.successful_transforms[-1] == ORIGINAL
    assert obs.closed is True


def _run_audio_loop(
    frames: Iterable[AudioFrame],
    step_factory: Callable[[FakeObs, DeterministicWait], Iterable[AudioQueueStep]],
    *,
    stop_observation: list[bool] | None = None,
) -> tuple[FakeObs, FakeAsr, DeterministicWait]:
    """Run one continuous real Runtime audio loop and controller thread."""

    item = make_capture_item(CAPTURE_DISPLAY, source_size=SOURCE)
    obs = FakeObs(item=item, original=ORIGINAL, canvas=CANVAS)
    obs.connect()
    pointer = FakePointer([CAPTURE_DISPLAY], position=(1440.0, 540.0))
    waiter = DeterministicWait()
    clock = DeterministicClock()
    frame_script = tuple(frames)
    asr = FakeAsr(
        frame_script,
        on_stop=(
            lambda: stop_observation.append(obs.original_written.is_set())
            if stop_observation is not None
            else None
        ),
    )
    config = load_config(Path("w11-007-missing-config.toml"))
    matcher = Matcher(config.commands)
    audio_queue = FakeAudioQueue(step_factory(obs, waiter))
    controller = ZoomController(
        obs=obs,
        item=item,
        orig=ORIGINAL,
        canvas=CANVAS,
        src=SOURCE,
        zoom_cfg=config.zoom,
        displays=[CAPTURE_DISPLAY],
        dry_run=False,
        pointer=pointer,
        clock=clock,
        wait=waiter,
        capture_display=CAPTURE_DISPLAY,
    )
    runtime = Runtime(
        _runtime_args(),
        RuntimeDependencies(clock=clock, wait=waiter),
    )
    interrupted = False

    try:
        controller.start()
        try:
            runtime._audio_loop(
                _runtime_args(), config, asr, matcher, controller, audio_queue
            )
        except KeyboardInterrupt:
            interrupted = True
    finally:
        # A failed queue/fake assertion must release a controller blocked at
        # the deterministic wait before public stop() joins it.
        waiter.release_all()
        controller.stop()

    assert interrupted is True
    assert controller.is_alive() is False
    assert waiter.errors == []
    return obs, asr, waiter


def test_audio_sequence_reaches_real_controller_and_restores_transform() -> None:
    """Fake audio/ASR reaches Runtime, Matcher, ZoomController, and fake OBS."""

    frames = (
        AudioFrame("來個特寫"),
        AudioFrame("來個特寫"),
        AudioFrame("來個特寫", endpoint=True),
        AudioFrame("退回全畫面"),
        AudioFrame(stop=True),
    )
    stop_observation: list[bool] = []

    def steps(obs: FakeObs, waiter: DeterministicWait) -> list[AudioQueueStep]:
        return [
            AudioQueueStep(0),
            AudioQueueStep(
                1,
                before_get=lambda: waiter.release_until(obs.non_original_written),
            ),
            AudioQueueStep(2),
            AudioQueueStep(3),
            AudioQueueStep(
                4,
                before_get=lambda: waiter.release_until(obs.original_written),
            ),
        ]

    obs, asr, waiter = _run_audio_loop(
        frames, steps, stop_observation=stop_observation
    )

    assert asr.calls == [0, 1, 2, 3, 4]
    assert waiter.errors == []
    assert stop_observation == [True]

    transforms = obs.successful_transforms
    first_restore = next(
        index for index, transform in enumerate(transforms) if transform == ORIGINAL
    )
    zoomed = transforms[:first_restore]
    assert zoomed
    assert transforms[-1] == ORIGINAL
    assert all(transform.scale_x > 1.0 for transform in zoomed)
    assert all(transform.scale_y == transform.scale_x for transform in zoomed)
    for transform in zoomed:
        assert transform == compute_transform(
            ORIGINAL,
            CANVAS,
            SOURCE,
            transform.scale_x,
            1440.0,
            540.0,
        )


def test_unrelated_audio_does_not_send_a_zoom_transform() -> None:
    frames = (
        AudioFrame("今天天氣真不錯", endpoint=True),
        AudioFrame(stop=True),
    )

    def steps(obs: FakeObs, waiter: DeterministicWait) -> list[AudioQueueStep]:
        del obs, waiter
        return [AudioQueueStep(0), AudioQueueStep(1)]

    obs, asr, _ = _run_audio_loop(frames, steps)

    assert asr.calls == [0, 1]
    assert obs.non_original_written.is_set() is False
    assert obs.successful_transforms
    assert all(transform == ORIGINAL for transform in obs.successful_transforms)


def test_duplicate_phrase_is_suppressed_by_matcher_inside_runtime_pipeline() -> None:
    """The same-utterance duplicate is observed through Runtime._audio_loop."""

    frames = (
        AudioFrame("來個特寫"),
        AudioFrame("退回全畫面"),
        AudioFrame("來個特寫"),
        AudioFrame(stop=True),
    )
    stop_observation: list[bool] = []

    def steps(obs: FakeObs, waiter: DeterministicWait) -> list[AudioQueueStep]:
        def release_one_tick_and_wait() -> None:
            previous = waiter.completed
            waiter.release()
            waiter.wait_for_completion(previous)

        return [
            AudioQueueStep(0),
            AudioQueueStep(
                1,
                before_get=lambda: waiter.release_until(obs.non_original_written),
            ),
            AudioQueueStep(
                2,
                before_get=lambda: waiter.release_until(obs.original_written),
            ),
            AudioQueueStep(3, before_get=release_one_tick_and_wait),
        ]

    obs, asr, waiter = _run_audio_loop(
        frames, steps, stop_observation=stop_observation
    )

    assert asr.calls == [0, 1, 2, 3]
    assert stop_observation == [True]
    assert waiter.errors == []
    transforms = obs.successful_transforms
    first_restore = next(
        index for index, transform in enumerate(transforms) if transform == ORIGINAL
    )
    assert any(transform != ORIGINAL for transform in transforms[:first_restore])
    assert all(transform == ORIGINAL for transform in transforms[first_restore:])


def _controller(
    pointer: FakePointer,
    displays: list[DisplayInfo],
    *,
    wait,
    capture_display: DisplayInfo | None = None,
) -> tuple[ZoomController, FakeObs]:
    item = make_capture_item(
        capture_display or displays[0],
        source_size=SOURCE,
    )
    obs = FakeObs(item=item, original=ORIGINAL, canvas=CANVAS)
    obs.connect()
    controller = ZoomController(
        obs=obs,
        item=item,
        orig=ORIGINAL,
        canvas=CANVAS,
        src=SOURCE,
        zoom_cfg=ZoomConfig(level=2.0, deadzone=0.15, smoothing=1.0),
        displays=displays,
        dry_run=False,
        pointer=pointer,
        clock=DeterministicClock(),
        wait=wait,
        capture_display=capture_display,
    )
    return controller, obs


def _finish_controller(controller: ZoomController) -> None:
    """Join a real controller and always release its stop path on assertion."""

    try:
        controller.join(timeout=2.0)
    finally:
        controller.stop()
    assert not controller.is_alive()


def test_pointer_tracking_honours_deadzone_before_following_capture_cursor() -> None:
    pointer = FakePointer([CAPTURE_DISPLAY], position=(960.0, 540.0))
    controller: ZoomController | None = None
    states: list[tuple[float, float]] = []

    def wait(seconds: float) -> None:
        del seconds
        states.append((controller._cur_cx, controller._cur_cy))  # type: ignore[union-attr]
        if len(states) == 1:
            pointer.set_position((1060.0, 540.0))
        elif len(states) == 2:
            pointer.set_position((1200.0, 540.0))
        else:
            controller._stop_event.set()  # type: ignore[union-attr]

    controller, obs = _controller(pointer, [CAPTURE_DISPLAY], wait=wait)
    controller.handle("zoom_in")
    controller.start()
    _finish_controller(controller)

    assert states == [(960.0, 540.0), (960.0, 540.0), (1056.0, 540.0)]
    zoomed = [transform for transform in obs.successful_transforms if transform != ORIGINAL]
    assert len(zoomed) == 3
    assert zoomed[0] == zoomed[1]
    assert zoomed[2] == compute_transform(
        ORIGINAL, CANVAS, SOURCE, 2.0, 1056.0, 540.0
    )
    assert obs.successful_transforms[-1] == ORIGINAL


def test_non_capture_monitor_freezes_center_during_zoom_animation() -> None:
    other_display = display(origin_x=1920.0, identifier="other-display")
    pointer = FakePointer(
        [CAPTURE_DISPLAY, other_display], position=(960.0, 540.0)
    )
    controller: ZoomController | None = None
    states: list[tuple[float, float]] = []

    def wait(seconds: float) -> None:
        del seconds
        states.append((controller._cur_cx, controller._cur_cy))  # type: ignore[union-attr]
        if len(states) == 1:
            pointer.set_position((2200.0, 540.0))
        else:
            controller._stop_event.set()  # type: ignore[union-attr]

    controller, obs = _controller(
        pointer,
        [CAPTURE_DISPLAY, other_display],
        wait=wait,
        capture_display=CAPTURE_DISPLAY,
    )
    controller.handle("zoom_in")
    controller.start()
    _finish_controller(controller)

    assert states == [(960.0, 540.0), (960.0, 540.0)]
    zoomed = [transform for transform in obs.successful_transforms if transform != ORIGINAL]
    assert len(zoomed) == 2
    assert zoomed[0] == zoomed[1]
    assert pointer.locate_calls[-1][0] == (2200.0, 540.0)
    assert obs.successful_transforms[-1] == ORIGINAL
