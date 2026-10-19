"""Unit tests for backend.stream_reader (no real cameras, no fixed sleeps)."""

from __future__ import annotations

import threading
import time
from typing import Any

import cv2
import numpy as np
import pytest

from backend.stream_reader import StreamReader, StreamStatus


def _wait_until(predicate: Any, timeout: float = 2.0, interval: float = 0.005) -> bool:
    """Poll predicate until True or timeout expires."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class FakeCapture:
    """Mock OpenCV VideoCapture for unit tests."""

    def __init__(
        self,
        frames: list[np.ndarray | None] | None = None,
        is_opened: bool = True,
        on_read: Any | None = None,
        continuous: bool = False,
    ) -> None:
        self._frames = list(frames) if frames is not None else []
        self._idx = 0
        self._is_opened = is_opened
        self.released = False
        self.set_calls: list[tuple[int, Any]] = []
        self.on_read = on_read
        self.continuous = continuous

    def isOpened(self) -> bool:
        return self._is_opened and not self.released

    def read(self) -> tuple[bool, np.ndarray | None]:
        if self.on_read is not None:
            self.on_read()
        if not self.isOpened() or (not self.continuous and self._idx >= len(self._frames)):
            return False, None
        if self.continuous and self._frames:
            frame = self._frames[min(self._idx, len(self._frames) - 1)]
            self._idx += 1
            return True, frame
        frame = self._frames[self._idx]
        self._idx += 1
        if frame is None:
            return False, None
        return True, frame

    def release(self) -> None:
        self.released = True

    def set(self, prop: int, value: Any) -> bool:
        self.set_calls.append((prop, value))
        if prop == cv2.CAP_PROP_POS_FRAMES and value == 0:
            self._idx = 0
            return True
        return True


def test_first_frame_status_live_and_latest_returns_copy() -> None:
    """First frame sets status LIVE and latest() returns a decoupled copy."""
    original = np.zeros((10, 10, 3), dtype=np.uint8)
    original[0, 0] = [10, 20, 30]

    cap = FakeCapture(frames=[original])
    reader = StreamReader(source=0, capture_factory=lambda _s: cap)
    reader.start()

    try:
        assert _wait_until(lambda: reader.status == StreamStatus.LIVE)
        result = reader.latest()
        assert result is not None
        frame_copy, ts = result
        assert isinstance(ts, float)
        assert np.array_equal(frame_copy, original)

        # Mutating the returned copy must not affect future latest() calls
        frame_copy[0, 0] = [99, 99, 99]
        result2 = reader.latest()
        assert result2 is not None
        frame_copy2, _ = result2
        assert frame_copy2[0, 0, 0] == 10
    finally:
        reader.stop()


def test_failed_open_then_success_reconnect_backoff_sequence() -> None:
    """Failed capture open retries with exponential backoff sequence 2, 4, 8, 10, 10."""
    delays_recorded: list[float] = []
    open_attempts = 0
    success_frame = np.ones((5, 5, 3), dtype=np.uint8)
    first_read_event = threading.Event()

    def fake_sleep(delay: float) -> None:
        delays_recorded.append(delay)

    def flaky_factory(_s: str | int) -> FakeCapture:
        nonlocal open_attempts
        open_attempts += 1
        if open_attempts <= 5:
            return FakeCapture(is_opened=False)
        return FakeCapture(
            frames=[success_frame],
            on_read=first_read_event.set,
            continuous=True,
        )

    reader = StreamReader(
        source=0,
        capture_factory=flaky_factory,
        sleep=fake_sleep,
        backoff_start=2.0,
        backoff_max=10.0,
    )
    reader.start()

    try:
        assert first_read_event.wait(timeout=2.0)
        assert _wait_until(lambda: reader.status == StreamStatus.LIVE)
        assert delays_recorded == [2.0, 4.0, 8.0, 10.0, 10.0]
    finally:
        reader.stop()


def test_offline_after_timeout_and_recovery_no_spam() -> None:
    """Missing frames for >= offline_after emits exactly one OFFLINE; recovery emits LIVE."""
    status_history: list[tuple[StreamStatus, StreamStatus]] = []
    current_time = [100.0]

    def fake_clock() -> float:
        return current_time[0]

    def fake_sleep(delay: float) -> None:
        current_time[0] += delay

    def on_status_change(old: StreamStatus, new: StreamStatus) -> None:
        status_history.append((old, new))

    phase = 1
    recovery_event = threading.Event()

    def controllable_factory(_s: str | int) -> FakeCapture:
        nonlocal phase
        if phase == 1:
            phase = 2
            # Delivers 1 frame then stream fails
            return FakeCapture(frames=[np.ones((4, 4, 3), dtype=np.uint8)])
        if phase == 2:
            # 3 failed opens
            phase = 3
            return FakeCapture(is_opened=False)
        if phase == 3:
            phase = 4
            return FakeCapture(is_opened=False)
        if phase == 4:
            phase = 5
            return FakeCapture(is_opened=False)
        # Phase 5: recovered
        return FakeCapture(
            frames=[np.ones((4, 4, 3), dtype=np.uint8)],
            on_read=recovery_event.set,
        )

    reader = StreamReader(
        source=0,
        capture_factory=controllable_factory,
        clock=fake_clock,
        sleep=fake_sleep,
        offline_after=5.0,
        backoff_start=2.0,
        backoff_max=10.0,
        on_status_change=on_status_change,
    )
    reader.start()

    try:
        assert recovery_event.wait(timeout=2.0)
        assert _wait_until(lambda: reader.status == StreamStatus.LIVE)

        expected = [
            (StreamStatus.CONNECTING, StreamStatus.LIVE),
            (StreamStatus.LIVE, StreamStatus.OFFLINE),
            (StreamStatus.OFFLINE, StreamStatus.LIVE),
        ]
        assert status_history == expected
    finally:
        reader.stop()


def test_loop_true_rewinds_and_keeps_live() -> None:
    """File source with loop=True rewinds at EOF and remains LIVE."""
    frame1 = np.ones((4, 4, 3), dtype=np.uint8)
    frame2 = np.ones((4, 4, 3), dtype=np.uint8) * 2
    cap = FakeCapture(frames=[frame1, frame2])

    reader = StreamReader(
        source="sample.mp4",
        loop=True,
        capture_factory=lambda _s: cap,
    )
    reader.start()

    try:
        # Verify reader rewinds to frame 0 and stays LIVE
        assert _wait_until(
            lambda: any(
                call[0] == cv2.CAP_PROP_POS_FRAMES and call[1] == 0
                for call in cap.set_calls
            )
        )
        assert reader.status == StreamStatus.LIVE
    finally:
        reader.stop()


def test_loop_false_at_eof_becomes_offline() -> None:
    """File source with loop=False transitions to OFFLINE at EOF."""
    frame1 = np.ones((4, 4, 3), dtype=np.uint8)
    cap = FakeCapture(frames=[frame1])

    reader = StreamReader(
        source="sample.mp4",
        loop=False,
        capture_factory=lambda _s: cap,
    )
    reader.start()

    try:
        assert _wait_until(lambda: reader.status == StreamStatus.OFFLINE)
    finally:
        reader.stop()


def test_stop_ends_thread_quickly_and_is_idempotent() -> None:
    """stop() cleanly stops the reader thread within 2s and can be called repeatedly."""
    cap = FakeCapture(frames=[np.ones((2, 2, 3), dtype=np.uint8)])
    reader = StreamReader(source=0, capture_factory=lambda _s: cap)
    reader.start()

    assert _wait_until(lambda: reader.status == StreamStatus.LIVE)

    start = time.monotonic()
    reader.stop(timeout=2.0)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0

    # Idempotent: repeated stop calls must not error
    reader.stop()
    reader.stop()


def test_callback_exception_does_not_kill_thread() -> None:
    """An exception raised inside on_status_change is logged and does not crash the thread."""
    first_frame_read = threading.Event()

    def faulty_callback(old: StreamStatus, new: StreamStatus) -> None:
        raise RuntimeError("Intentional callback explosion")

    cap = FakeCapture(
        frames=[np.ones((2, 2, 3), dtype=np.uint8)],
        on_read=first_frame_read.set,
    )
    reader = StreamReader(
        source=0,
        capture_factory=lambda _s: cap,
        on_status_change=faulty_callback,
    )
    reader.start()

    try:
        assert first_frame_read.wait(timeout=2.0)
        assert _wait_until(lambda: reader.status == StreamStatus.LIVE)
        assert reader.latest() is not None
    finally:
        reader.stop()


def test_fps_computed_from_fake_clock_arrivals() -> None:
    """FPS is accurately computed from rolling frame arrival timestamps."""
    current_time = [100.0]

    def fake_clock() -> float:
        return current_time[0]

    frame_count = 0
    all_read = threading.Event()

    def on_frame_read() -> None:
        nonlocal frame_count
        frame_count += 1
        current_time[0] += 0.1  # 10 fps
        if frame_count >= 5:
            all_read.set()

    frames = [np.ones((2, 2, 3), dtype=np.uint8) for _ in range(5)]
    cap = FakeCapture(frames=frames, on_read=on_frame_read)

    reader = StreamReader(
        source=0,
        capture_factory=lambda _s: cap,
        clock=fake_clock,
    )
    reader.start()

    try:
        assert all_read.wait(timeout=2.0)
        # 5 frames across 4 intervals of 0.1s -> 4 / 0.4 = 10.0 FPS
        assert _wait_until(lambda: abs(reader.fps - 10.0) < 0.1)
    finally:
        reader.stop()


def test_last_frame_age() -> None:
    """last_frame_age returns None initially, then elapsed seconds since last frame."""
    current_time = [50.0]

    def fake_clock() -> float:
        return current_time[0]

    reader = StreamReader(source=0, clock=fake_clock)
    assert reader.last_frame_age() is None

    cap = FakeCapture(frames=[np.ones((2, 2, 3), dtype=np.uint8)])
    reader = StreamReader(source=0, capture_factory=lambda _s: cap, clock=fake_clock)
    reader.start()

    try:
        assert _wait_until(lambda: reader.latest() is not None)
        assert reader.last_frame_age(now=50.5) == pytest.approx(0.5)
    finally:
        reader.stop()
