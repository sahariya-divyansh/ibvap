"""Tests for camera health verification script [D10]."""

from __future__ import annotations

import numpy as np

from scripts.check_cameras import check_source, sanitize_source


class FakeCapture:
    """Fake cv2.VideoCapture implementation for script unit testing."""

    def __init__(
        self,
        is_open: bool = True,
        frames: list[tuple[bool, np.ndarray | None]] | None = None,
    ) -> None:
        self._is_open = is_open
        self._frames = (
            list(frames)
            if frames is not None
            else [(True, np.zeros((480, 640, 3), dtype=np.uint8))]
        )
        self.released = False

    def isOpened(self) -> bool:
        return self._is_open

    def read(self) -> tuple[bool, np.ndarray | None]:
        if self._frames:
            return self._frames.pop(0)
        return False, None

    def release(self) -> None:
        self.released = True


def test_check_source_ok_case() -> None:
    """A working capture returns (True, resolution) and releases capture."""
    dummy_frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    capture = FakeCapture(is_open=True, frames=[(True, dummy_frame)])

    ok, res = check_source(lambda _s: capture, "http://192.168.1.5:8080/video", timeout=1.0)
    assert ok is True
    assert res == "1280x720"
    assert capture.released is True


def test_check_source_open_failure_case() -> None:
    """A capture that fails to open returns (False, 'N/A')."""
    capture = FakeCapture(is_open=False)

    ok, res = check_source(lambda _s: capture, 0, timeout=1.0)
    assert ok is False
    assert res == "N/A"


def test_check_source_no_frame_timeout_case() -> None:
    """A capture that opens but produces no valid frames within timeout returns (False, 'N/A')."""
    capture = FakeCapture(is_open=True, frames=[(False, None), (False, None)])

    time_counter = [0.0]

    def fake_clock() -> float:
        time_counter[0] += 0.5
        return time_counter[0]

    ok, res = check_source(
        lambda _s: capture,
        "sample_data/clip.mp4",
        timeout=1.0,
        clock=fake_clock,
        sleep=lambda _s: None,
    )
    assert ok is False
    assert res == "N/A"
    assert capture.released is True


def test_sanitize_source_masks_credentials() -> None:
    """Sensitive credentials in RTSP/HTTP URLs are stripped."""
    assert (
        sanitize_source("http://admin:secret123@192.168.1.100:8080/video")
        == "http://192.168.1.100:8080/video"
    )
    assert sanitize_source("rtsp://user:pass@10.0.0.5/h264") == "rtsp://10.0.0.5/h264"
    assert (
        sanitize_source("http://192.168.1.100:8080/video")
        == "http://192.168.1.100:8080/video"
    )
    assert sanitize_source(0) == "0"
