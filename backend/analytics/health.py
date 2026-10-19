
"""Camera frame health and tamper monitor.

Owner: Kasumi (S1).
"""

from __future__ import annotations

import cv2
import numpy as np

from backend.analytics.types import EventCandidate, EventType


class FrameHealthMonitor:
    """Detect persistent blur, blackout, and frozen camera frames."""

    def __init__(
        self,
        check_interval: float = 1.0,
        warmup_seconds: float = 10.0,
        blur_ratio: float = 0.3,
        dark_mean: float = 25.0,
        frozen_seconds: float = 4.0,
        confirm_checks: int = 3,
    ) -> None:
        if check_interval <= 0:
            raise ValueError("check_interval must be positive")
        if warmup_seconds < 0:
            raise ValueError("warmup_seconds cannot be negative")
        if not 0 < blur_ratio <= 1:
            raise ValueError("blur_ratio must be in (0, 1]")
        if dark_mean < 0:
            raise ValueError("dark_mean cannot be negative")
        if frozen_seconds <= 0:
            raise ValueError("frozen_seconds must be positive")
        if confirm_checks < 1:
            raise ValueError("confirm_checks must be at least 1")

        self.check_interval = check_interval
        self.warmup_seconds = warmup_seconds
        self.blur_ratio = blur_ratio
        self.dark_mean = dark_mean
        self.frozen_seconds = frozen_seconds
        self.confirm_checks = confirm_checks

        self._started_at: float | None = None
        self._last_check: float | None = None
        self._blur_samples: list[float] = []
        self._blur_baseline: float | None = None

        self._previous_gray: np.ndarray | None = None
        self._previous_frame_at: float | None = None
        self._unchanged_since: float | None = None

        self._bad_counts = {
            "BLUR": 0,
            "BLACKOUT": 0,
            "FROZEN": 0,
        }
        self._good_counts = {
            "BLUR": 0,
            "BLACKOUT": 0,
            "FROZEN": 0,
        }
        self._active = {
            "BLUR": False,
            "BLACKOUT": False,
            "FROZEN": False,
        }

    @staticmethod
    def _to_gray(frame: np.ndarray) -> np.ndarray | None:
        """Validate the frame and resize it to a small grayscale image."""
        if not isinstance(frame, np.ndarray) or frame.size == 0:
            return None
        if frame.dtype != np.uint8 or frame.ndim not in (2, 3):
            return None

        if frame.ndim == 3:
            if frame.shape[2] == 1:
                frame = frame[:, :, 0]
            elif frame.shape[2] == 3:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            elif frame.shape[2] == 4:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY)
            else:
                return None

        if frame.shape[0] < 2 or frame.shape[1] < 2:
            return None

        height, width = frame.shape
        small_width = min(160, width)
        small_height = max(1, round(height * small_width / width))

        if (small_width, small_height) != (width, height):
            frame = cv2.resize(
                frame,
                (small_width, small_height),
                interpolation=cv2.INTER_AREA,
            )

        return np.ascontiguousarray(frame)

    def _record_condition(
        self,
        kind: str,
        bad: bool,
        score: float,
    ) -> list[EventCandidate]:
        """Confirm a problem before emitting an event; confirm recovery too."""
        if bad:
            self._bad_counts[kind] += 1
            self._good_counts[kind] = 0

            if (
                not self._active[kind]
                and self._bad_counts[kind] >= self.confirm_checks
            ):
                self._active[kind] = True
                return [
                    EventCandidate(
                        event_type=EventType.CAMERA_TAMPER,
                        extra={"kind": kind, "score": float(score)},
                    )
                ]
        else:
            self._bad_counts[kind] = 0

            if self._active[kind]:
                self._good_counts[kind] += 1
                if self._good_counts[kind] >= self.confirm_checks:
                    self._active[kind] = False
                    self._good_counts[kind] = 0
            else:
                self._good_counts[kind] = 0

        return []

    def update(self, frame: np.ndarray, now: float) -> list[EventCandidate]:
        """Check frame health at the configured interval."""
        gray = self._to_gray(frame)
        if gray is None:
            return []

        if self._started_at is None:
            self._started_at = now

        if self._last_check is not None:
            if now < self._last_check:
                return []
            if now - self._last_check < self.check_interval:
                return []

        previous_check_at = self._last_check
        self._last_check = now
        elapsed = now - self._started_at

        mean_brightness = float(np.mean(gray))
        brightness_std = float(np.std(gray))
        laplacian_score = float(cv2.Laplacian(gray, cv2.CV_64F).var())

        # A dark, nearly uniform image indicates a possible blackout.
        # Dark scenes with texture should not trigger this condition.
        blackout_bad = (
            mean_brightness < self.dark_mean and brightness_std < 5.0
        )

        # Learn the normal sharpness level during the warmup period.
        if elapsed <= self.warmup_seconds:
            if mean_brightness >= self.dark_mean and brightness_std >= 5.0:
                self._blur_samples.append(laplacian_score)
        elif self._blur_baseline is None and self._blur_samples:
            baseline = float(np.median(self._blur_samples))
            if baseline > 0:
                self._blur_baseline = baseline

        blur_bad = (
            elapsed > self.warmup_seconds
            and self._blur_baseline is not None
            and laplacian_score < self.blur_ratio * self._blur_baseline
        )

        # Detect repeated frames while allowing tiny compression changes.
        frozen_bad = False
        if self._previous_gray is not None:
            difference = float(
                np.mean(cv2.absdiff(gray, self._previous_gray))
            )

            if difference <= 1.0:
                if self._unchanged_since is None:
                    self._unchanged_since = previous_check_at
                if self._unchanged_since is not None:
                    frozen_bad = (
                        now - self._unchanged_since >= self.frozen_seconds
                    )
            else:
                self._unchanged_since = None

        self._previous_gray = gray.copy()
        self._previous_frame_at = now

        events: list[EventCandidate] = []
        events.extend(
            self._record_condition("BLUR", blur_bad, laplacian_score)
        )
        events.extend(
            self._record_condition("BLACKOUT", blackout_bad, mean_brightness)
        )
        events.extend(
            self._record_condition("FROZEN", frozen_bad, 0.0)
        )
        return events
