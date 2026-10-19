
import numpy as np
import pytest

from backend.analytics.health import FrameHealthMonitor


@pytest.mark.parametrize(
    "frame",
    [
        None,
        np.array([], dtype=np.uint8),
        np.array([1, 2, 3], dtype=np.float64),
    ],
)
def test_invalid_frame_returns_empty_list(frame):
    monitor = FrameHealthMonitor()

    result = monitor.update(frame, now=100.0)

    assert result == []


def test_blur_is_detected_after_confirmation():
    rng = np.random.default_rng(42)
    monitor = FrameHealthMonitor(
        check_interval=1.0,
        warmup_seconds=3.0,
        confirm_checks=2,
    )

    sharp = rng.integers(0, 256, (120, 160, 3), dtype=np.uint8)
    blurred = np.full((120, 160, 3), 128, dtype=np.uint8)

    events = []
    for second in range(4):
        events.extend(monitor.update(sharp, now=float(second)))

    for second in range(4, 12):
        events.extend(monitor.update(blurred, now=float(second)))

    blur_events = [
        event for event in events
        if event.extra.get("kind") == "BLUR"
    ]

    assert len(blur_events) == 1

def test_blackout_is_detected_and_can_trigger_again():
    monitor = FrameHealthMonitor(
        check_interval=1.0,
        warmup_seconds=10.0,
        confirm_checks=2,
    )

    black = np.zeros((120, 160, 3), dtype=np.uint8)
    normal = np.full((120, 160, 3), 128, dtype=np.uint8)

    events = []
    for second in range(3):
        events.extend(monitor.update(black, now=float(second)))

    blackout_events = [
        event for event in events
        if event.extra.get("kind") == "BLACKOUT"
    ]
    assert len(blackout_events) == 1

    for second in range(3, 6):
        monitor.update(normal, now=float(second))

    events = []
    for second in range(6, 9):
        events.extend(monitor.update(black, now=float(second)))

    blackout_events = [
        event for event in events
        if event.extra.get("kind") == "BLACKOUT"
    ]
    assert len(blackout_events) == 1

def test_frozen_camera_is_detected_once():
    monitor = FrameHealthMonitor(
        check_interval=1.0,
        frozen_seconds=3.0,
        confirm_checks=2,
    )

    frame = np.random.default_rng(42).integers(
        0, 256, (120, 160, 3), dtype=np.uint8
    )

    events = []
    for second in range(8):
        events.extend(monitor.update(frame, now=float(second)))

    frozen_events = [
        event for event in events
        if event.extra.get("kind") == "FROZEN"
    ]

    assert len(frozen_events) == 1

def test_slightly_noisy_frames_do_not_trigger_frozen():
    monitor = FrameHealthMonitor(
        check_interval=1.0,
        frozen_seconds=3.0,
        confirm_checks=2,
    )

    base = np.full((120, 160, 3), 128, dtype=np.uint8)
    rng = np.random.default_rng(42)

    events = []
    for second in range(8):
        noise = rng.integers(-2, 3, base.shape)
        frame = np.clip(
            base.astype(np.int16) + noise,
            0,
            255,
        ).astype(np.uint8)

        events.extend(monitor.update(frame, now=float(second)))

    frozen_events = [
        event for event in events
        if event.extra.get("kind") == "FROZEN"
    ]

    assert len(frozen_events) == 0

def test_dark_textured_scene_does_not_trigger_blackout():
    monitor = FrameHealthMonitor(
        check_interval=1.0,
        dark_mean=25.0,
        confirm_checks=2,
    )

    rng = np.random.default_rng(42)
    frame = rng.integers(
        0, 40, (120, 160, 3), dtype=np.uint8
    )

    events = []
    for second in range(8):
        events.extend(monitor.update(frame, now=float(second)))

    blackout_events = [
        event for event in events
        if event.extra.get("kind") == "BLACKOUT"
    ]

    assert len(blackout_events) == 0

def test_frozen_camera_can_trigger_again_after_recovery():
    monitor = FrameHealthMonitor(
        check_interval=1.0,
        frozen_seconds=3.0,
        confirm_checks=2,
    )

    frozen_frame = np.full((120, 160, 3), 100, dtype=np.uint8)
    normal_frame = np.random.default_rng(42).integers(
        0, 256, (120, 160, 3), dtype=np.uint8
    )

    events = []
    for second in range(5):
        events.extend(monitor.update(frozen_frame, now=float(second)))

    first_events = [
        event for event in events
        if event.extra.get("kind") == "FROZEN"
    ]
    assert len(first_events) == 1

    for second in range(5, 8):
        monitor.update(normal_frame, now=float(second))

    events = []
    for second in range(8, 13):
        events.extend(monitor.update(frozen_frame, now=float(second)))

    second_events = [
        event for event in events
        if event.extra.get("kind") == "FROZEN"
    ]
    assert len(second_events) == 1


def test_blur_can_trigger_again_after_recovery():
    monitor = FrameHealthMonitor(
        check_interval=1.0,
        warmup_seconds=2.0,
        confirm_checks=2,
    )

    rng = np.random.default_rng(42)
    sharp = rng.integers(0, 256, (120, 160, 3), dtype=np.uint8)
    blurred = np.full((120, 160, 3), 128, dtype=np.uint8)

    events = []
    for second in range(3):
        events.extend(monitor.update(sharp, now=float(second)))

    for second in range(3, 6):
        events.extend(monitor.update(blurred, now=float(second)))

    first_blur_events = [
        event for event in events
        if event.extra.get("kind") == "BLUR"
    ]
    assert len(first_blur_events) == 1

    for second in range(6, 9):
        monitor.update(sharp, now=float(second))

    events = []
    for second in range(9, 12):
        events.extend(monitor.update(blurred, now=float(second)))

    second_blur_events = [
        event for event in events
        if event.extra.get("kind") == "BLUR"
    ]
    assert len(second_blur_events) == 1
