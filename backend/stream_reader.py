"""Threaded stream reader with auto-reconnect, latest-frame buffering, and status tracking."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from enum import Enum
from typing import Any

import cv2
import numpy as np

from backend.utils.video_source import parse_video_source

LOGGER = logging.getLogger(__name__)


class StreamStatus(str, Enum):
    """Lifecycle status of a camera video stream."""

    CONNECTING = "CONNECTING"
    LIVE = "LIVE"
    OFFLINE = "OFFLINE"


def _is_file_source(source: str | int) -> bool:
    """Return True if source represents a local file path rather than a device or URL."""
    if isinstance(source, int):
        return False
    stripped = source.strip()
    if stripped.isdigit():
        return False
    lower = stripped.lower()
    return not lower.startswith(("http://", "https://", "rtsp://", "rtsps://"))


class StreamReader:
    """Threaded OpenCV video capture reader keeping only the latest frame.

    # Thread safety: All shared state between the reader thread and public API
    # callers is synchronized behind self._lock.
    """

    def __init__(
        self,
        source: str | int,
        *,
        loop: bool = False,
        capture_factory: Callable[[str | int], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        offline_after: float = 5.0,
        backoff_start: float = 2.0,
        backoff_max: float = 10.0,
        on_status_change: Callable[[StreamStatus, StreamStatus], None] | None = None,
    ) -> None:
        """Initialize the stream reader."""
        self._source = source
        self._loop = loop
        self._is_file = _is_file_source(source)
        self._capture_factory = (
            capture_factory if capture_factory is not None else self._default_capture_factory
        )
        self._clock = clock
        self._sleep = sleep
        self._offline_after = offline_after
        self._backoff_start = backoff_start
        self._backoff_max = backoff_max
        self._on_status_change = on_status_change

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False

        # State protected by self._lock
        self._status = StreamStatus.CONNECTING
        self._latest_frame: np.ndarray | None = None
        self._latest_ts: float | None = None
        self._frame_times: deque[float] = deque()
        self._capture: Any = None

    @staticmethod
    def _default_capture_factory(source: str | int) -> Any:
        """Create a cv2.VideoCapture with buffer size 1."""
        target: int | str
        if isinstance(source, int):
            target = source
        else:
            target = parse_video_source(source)
        cap = cv2.VideoCapture(target)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except (cv2.error, AttributeError, RuntimeError):
            LOGGER.debug("Failed to set buffer size on capture")
        return cap

    @property
    def source(self) -> str | int:
        """Return the configured video source."""
        return self._source

    @property
    def status(self) -> StreamStatus:
        """Return the current stream lifecycle status."""
        with self._lock:
            return self._status

    @property
    def fps(self) -> float:
        """Return the rolling arrival frame rate over the last ~2 seconds."""
        with self._lock:
            now = self._clock()
            while self._frame_times and self._frame_times[0] < now - 2.0:
                self._frame_times.popleft()
            if len(self._frame_times) < 2:
                return 0.0
            dt = self._frame_times[-1] - self._frame_times[0]
            if dt <= 0.0:
                return 0.0
            return (len(self._frame_times) - 1) / dt

    def latest(self) -> tuple[np.ndarray, float] | None:
        """Return a copy of the latest frame and its capture timestamp, or None."""
        with self._lock:
            if self._latest_frame is None or self._latest_ts is None:
                return None
            return (self._latest_frame.copy(), self._latest_ts)

    def last_frame_age(self, now: float | None = None) -> float | None:
        """Return seconds elapsed since the latest frame was captured, or None."""
        with self._lock:
            if self._latest_ts is None:
                return None
            current_time = self._clock() if now is None else now
            return max(0.0, current_time - self._latest_ts)

    def start(self) -> None:
        """Launch the background capture daemon thread (idempotent)."""
        with self._lock:
            if self._started and self._thread is not None and self._thread.is_alive():
                return
            self._started = True
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name=f"StreamReader-{self._source}",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the background capture thread and release capture resources (idempotent)."""
        with self._lock:
            if not self._started and self._thread is None:
                return
            self._stop_event.set()
            thread = self._thread
            self._started = False

        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

        with self._lock:
            self._thread = None
            if self._capture is not None:
                self._release_capture(self._capture)
                self._capture = None

    def _set_status(self, new_status: StreamStatus) -> None:
        """Transition stream status and notify callback outside the lock."""
        with self._lock:
            old_status = self._status
            if old_status == new_status:
                return
            self._status = new_status

        if self._on_status_change is not None:
            try:
                self._on_status_change(old_status, new_status)
            except Exception:
                LOGGER.exception(
                    "Error in on_status_change callback (%s -> %s)",
                    old_status,
                    new_status,
                )

    def _wait(self, delay: float) -> bool:
        """Wait for delay seconds or until stop_event is set.

        Uses self._sleep to allow test instrumentation, and checks stop_event.
        Returns True if stop was requested, False otherwise.
        """
        if self._sleep is time.sleep:
            return self._stop_event.wait(delay)

        self._sleep(delay)
        return self._stop_event.is_set()

    @staticmethod
    def _release_capture(capture: Any) -> None:
        """Safely release an open capture object."""
        if capture is not None:
            try:
                capture.release()
            except (cv2.error, AttributeError, RuntimeError):
                LOGGER.debug("Failed to release capture")

    def _check_offline(self, start_time: float) -> None:
        """Check if offline_after has elapsed without frames and update status."""
        with self._lock:
            now = self._clock()
            last_time = self._latest_ts if self._latest_ts is not None else start_time
            if now - last_time >= self._offline_after:
                is_offline = True
            else:
                is_offline = False
        if is_offline:
            self._set_status(StreamStatus.OFFLINE)

    def _run(self) -> None:
        """Continuously capture frames and maintain latest-frame buffer."""
        backoff = self._backoff_start
        start_time = self._clock()
        capture: Any = None

        while not self._stop_event.is_set():
            if capture is None:
                self._check_offline(start_time)

                try:
                    cap = self._capture_factory(self._source)
                    is_opened = cap.isOpened() if hasattr(cap, "isOpened") else True
                except (cv2.error, OSError, RuntimeError, ValueError, TypeError, AttributeError) as exc:
                    LOGGER.debug("Failed to initialize capture: %s", exc)
                    cap = None
                    is_opened = False

                if not is_opened:
                    self._release_capture(cap)
                    self._check_offline(start_time)
                    if self._wait(backoff):
                        break
                    backoff = min(backoff * 2.0, self._backoff_max)
                    continue

                capture = cap
                with self._lock:
                    self._capture = capture

            # Read frame
            try:
                ok, frame = capture.read()
            except (cv2.error, OSError, RuntimeError, ValueError, TypeError, AttributeError) as exc:
                LOGGER.debug("Failed to read from capture: %s", exc)
                ok, frame = False, None

            if not ok or frame is None:
                # Handle file loop
                if self._is_file and self._loop:
                    try:
                        capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        ok, frame = capture.read()
                    except (cv2.error, OSError, RuntimeError, ValueError, TypeError, AttributeError) as exc:
                        LOGGER.debug("Failed to rewind capture: %s", exc)
                        ok, frame = False, None

                if not ok or frame is None:
                    # File source at EOF with loop=False transitions to OFFLINE and stops reading
                    if self._is_file and not self._loop:
                        self._set_status(StreamStatus.OFFLINE)
                        self._release_capture(capture)
                        with self._lock:
                            self._capture = None
                        self._stop_event.wait()
                        break

                    # Failed read on live source or looping file: release and reconnect with backoff
                    self._release_capture(capture)
                    with self._lock:
                        self._capture = None
                    capture = None

                    self._check_offline(start_time)
                    if self._wait(backoff):
                        break
                    backoff = min(backoff * 2.0, self._backoff_max)
                    continue

            # Frame read succeeded
            now = self._clock()
            with self._lock:
                self._latest_frame = frame
                self._latest_ts = now
                self._frame_times.append(now)
                while self._frame_times and self._frame_times[0] < now - 2.0:
                    self._frame_times.popleft()

            self._set_status(StreamStatus.LIVE)
            backoff = self._backoff_start

        # Clean exit
        if capture is not None:
            self._release_capture(capture)
            with self._lock:
                self._capture = None
