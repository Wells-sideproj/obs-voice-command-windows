from __future__ import annotations

from argparse import Namespace
from collections.abc import Callable, Iterable
from pathlib import Path
import threading

from obs_voice_command.config import ZoomConfig, load_config
from obs_voice_command.matcher import Matcher
from obs_voice_command.platform.common import DisplayInfo
from obs_voice_command.runtime import Runtime, RuntimeDependencies, ZoomController
from obs_voice_command.zoom import Transform
from tests.fakes.application import (
    AudioFrame,
    AudioQueueStep,
    DeterministicClock,
    DeterministicWait,
    FakeAsr,
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


def _controller(
    wait: DeterministicWait,
    *,
    fail_first_non_original: bool = False,
    fail_reconnect_attempts: int = 0,
    fail_first_reconnect_baseline: bool = False,
) -> tuple[ZoomController, FakeObs]:
    item = make_capture_item(CAPTURE_DISPLAY, source_size=SOURCE)
    obs = FakeObs(
        item=item,
        original=ORIGINAL,
        canvas=CANVAS,
        fail_first_non_original=fail_first_non_original,
        fail_reconnect_attempts=fail_reconnect_attempts,
        fail_first_reconnect_baseline=fail_first_reconnect_baseline,
    )
    obs.connect()
    pointer = FakePointer([CAPTURE_DISPLAY], position=(1440.0, 540.0))
    controller = ZoomController(
        obs=obs,
        item=item,
        orig=ORIGINAL,
        canvas=CANVAS,
        src=SOURCE,
        zoom_cfg=ZoomConfig(level=2.0, deadzone=0.15, smoothing=1.0),
        displays=[CAPTURE_DISPLAY],
        dry_run=False,
        pointer=pointer,
        clock=DeterministicClock(),
        wait=wait,
    )
    return controller, obs


def test_disconnect_reconnect_restores_original_before_controller_is_idle() -> None:
    waiter = DeterministicWait()
    controller, obs = _controller(
        waiter,
        fail_first_non_original=True,
        fail_reconnect_attempts=1,
        fail_first_reconnect_baseline=True,
    )
    idle_during_reconnect_restore: list[bool] = []

    def observe_transform(transform: Transform) -> None:
        if transform == ORIGINAL and not idle_during_reconnect_restore:
            idle_during_reconnect_restore.append(controller._idle)

    obs.on_set_transform = observe_transform
    try:
        controller.handle("zoom_in")
        controller.start()
        waiter.release_until(obs.original_written)
    finally:
        # Always release a controller blocked in reconnect or at its next tick.
        waiter.release_all()
        controller.stop()

    assert controller.is_alive() is False
    assert waiter.errors == []
    assert obs.connect_attempts == 4
    assert obs.connections == 3
    assert idle_during_reconnect_restore == [False]
    assert controller._target_z == 1.0
    assert controller._idle is True
    assert obs.successful_transforms == [ORIGINAL, ORIGINAL]

    disconnect_index = next(
        index for index, event in enumerate(obs.events) if event[0] == "disconnect"
    )
    failed_reconnect_index = next(
        index
        for index, event in enumerate(obs.events)
        if index > disconnect_index and event[0] == "connect_failed"
    )
    first_reconnect_index = next(
        index
        for index, event in enumerate(obs.events)
        if index > failed_reconnect_index and event[0] == "connect"
    )
    baseline_failure_index = next(
        index
        for index, event in enumerate(obs.events)
        if index > first_reconnect_index and event[0] == "baseline_failed"
    )
    final_reconnect_index = next(
        index
        for index, event in enumerate(obs.events)
        if index > baseline_failure_index and event[0] == "connect"
    )
    restored_index = next(
        index
        for index, event in enumerate(obs.events)
        if index > final_reconnect_index and event == ("set", ORIGINAL)
    )
    assert (
        disconnect_index
        < failed_reconnect_index
        < first_reconnect_index
        < baseline_failure_index
        < final_reconnect_index
        < restored_index
    )


def test_normal_stop_restores_original_while_controller_is_active() -> None:
    waiter = DeterministicWait()
    controller, obs = _controller(waiter)
    stop_thread: threading.Thread | None = None
    stop_finished = threading.Event()
    stop_errors: list[BaseException] = []

    try:
        controller.handle("zoom_in")
        controller.start()
        waiter.release_until(obs.non_original_written)
        assert controller.is_alive() is True
        transforms_before_stop = list(obs.successful_transforms)

        def invoke_public_stop() -> None:
            try:
                controller.stop()
            except BaseException as exc:
                stop_errors.append(exc)
            finally:
                stop_finished.set()

        stop_thread = threading.Thread(
            target=invoke_public_stop,
            name="fake-normal-stop",
            daemon=True,
        )
        stop_thread.start()
        # Wait for production stop() to set its state before releasing the
        # injected controller wait. This is the actual shutdown handshake.
        assert controller._stop_event.wait(timeout=2.0)
        waiter.release()
        assert stop_finished.wait(2.0)
        stop_thread.join(timeout=2.0)
        assert stop_thread.is_alive() is False
        assert controller.is_alive() is False
        assert stop_errors == []

        assert obs.successful_transforms[: len(transforms_before_stop)] == transforms_before_stop
        assert obs.successful_transforms[-1] == ORIGINAL
        writes_after_first_stop = list(obs.successful_transforms)

        # A second public stop is idempotent and must not append another write.
        controller.stop()
        controller._send_transform(Transform(100.0, 100.0, 2.0, 2.0))
        assert obs.successful_transforms == writes_after_first_stop
    finally:
        # This path also handles a fake/handshake assertion before stop starts.
        waiter.release_all()
        if stop_thread is not None and stop_thread.is_alive():
            stop_thread.join(timeout=2.0)
        if controller.is_alive():
            controller.stop()

    assert waiter.errors == []
    assert stop_errors == []
    assert obs.successful_transforms[-1] == ORIGINAL


def _run_runtime_audio_loop(
    frames: Iterable[AudioFrame],
    step_factory: Callable[[FakeObs, DeterministicWait], Iterable[AudioQueueStep]],
    *,
    stop_observation: list[bool],
) -> tuple[FakeObs, FakeAsr, DeterministicWait]:
    item = make_capture_item(CAPTURE_DISPLAY, source_size=SOURCE)
    obs = FakeObs(item=item, original=ORIGINAL, canvas=CANVAS)
    obs.connect()
    pointer = FakePointer([CAPTURE_DISPLAY], position=(1440.0, 540.0))
    waiter = DeterministicWait()
    clock = DeterministicClock()
    frame_script = tuple(frames)
    asr = FakeAsr(
        frame_script,
        on_stop=lambda: stop_observation.append(obs.original_written.is_set()),
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
        Namespace(
            config="w11-007-missing-config.toml",
            dry_run=False,
            os=False,
            list_devices=False,
        ),
        RuntimeDependencies(clock=clock, wait=waiter),
    )
    interrupted = False

    try:
        controller.start()
        try:
            runtime._audio_loop(
                runtime.args,
                config,
                asr,
                matcher,
                controller,
                audio_queue,
            )
        except KeyboardInterrupt:
            interrupted = True
    finally:
        waiter.release_all()
        controller.stop()

    assert interrupted is True
    assert controller.is_alive() is False
    assert waiter.errors == []
    return obs, asr, waiter


def test_ctrl_c_restores_original_through_runtime_audio_cleanup() -> None:
    frames = (
        AudioFrame("來個特寫"),
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
        ]

    obs, asr, waiter = _run_runtime_audio_loop(
        frames, steps, stop_observation=stop_observation
    )

    assert asr.calls == [0, 1]
    assert stop_observation == [False]
    assert obs.non_original_written.is_set()
    assert obs.successful_transforms[-1] == ORIGINAL
    assert waiter.errors == []
