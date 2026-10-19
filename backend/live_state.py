"""Thread-safe per-camera live detector state shared with API routes."""

from __future__ import annotations

import threading
from typing import Any

import cv2
import numpy as np


class CameraLiveState:
    """Live state for a single camera stream."""

    __slots__ = (
        "annotated_frame",
        "annotated_jpeg",
        "counts",
        "fps",
        "last_frame_ts",
        "latency_ms",
        "raw_frame",
        "status",
    )

    def __init__(
        self,
        annotated_frame: np.ndarray | None = None,
        annotated_jpeg: bytes | None = None,
        raw_frame: np.ndarray | None = None,
        counts: dict[str, int] | None = None,
        status: str = "OFFLINE",
        fps: float = 0.0,
        latency_ms: float = 0.0,
        last_frame_ts: float | None = None,
    ) -> None:
        self.annotated_frame = annotated_frame
        self.annotated_jpeg = annotated_jpeg
        self.raw_frame = raw_frame
        self.counts = counts or {"person": 0, "vehicle": 0, "animal": 0}
        self.status = status
        self.fps = fps
        self.latency_ms = latency_ms
        self.last_frame_ts = last_frame_ts

    def to_dict(self) -> dict[str, Any]:
        """Convert state to a plain dictionary for API responses."""
        return {
            "annotated_jpeg": self.annotated_jpeg,
            "raw_frame": self.raw_frame.copy() if self.raw_frame is not None else None,
            "counts": dict(self.counts),
            "status": self.status,
            "fps": self.fps,
            "latency_ms": self.latency_ms,
            "last_frame_ts": self.last_frame_ts,
        }


class LiveDetectorState:
    """Store per-camera live state (frames, counts, metrics) behind one lock."""

    def __init__(self) -> None:
        """Create an empty multi-camera live state registry."""
        self._lock = threading.Lock()
        self._cameras: dict[str, CameraLiveState] = {}
        self._camera_order: list[str] = []

    def _first_camera_id(self) -> str | None:
        """Return the first registered camera_id, if any."""
        return self._camera_order[0] if self._camera_order else None

    def publish(
        self,
        camera_id: str,
        annotated_frame: np.ndarray,
        counts: dict[str, int],
        status: str = "LIVE",
        fps: float = 0.0,
        latency_ms: float = 0.0,
        last_frame_ts: float | None = None,
        raw_frame: np.ndarray | None = None,
        annotated_jpeg: bytes | None = None,
    ) -> None:
        """Atomically publish new frame and metrics for a camera.

        JPEG encoding is performed outside the lock so readers never block on encoding.
        """
        # Pre-encode JPEG outside the lock if not already provided
        if annotated_jpeg is None and annotated_frame is not None:
            success, encoded = cv2.imencode(".jpg", annotated_frame)
            if success:
                annotated_jpeg = encoded.tobytes()

        clean_counts = {
            "person": int(counts.get("person", 0)),
            "vehicle": int(counts.get("vehicle", 0)),
            "animal": int(counts.get("animal", 0)),
        }
        annotated_copy = annotated_frame.copy() if annotated_frame is not None else None
        raw_copy = raw_frame.copy() if raw_frame is not None else None

        state = CameraLiveState(
            annotated_frame=annotated_copy,
            annotated_jpeg=annotated_jpeg,
            raw_frame=raw_copy,
            counts=clean_counts,
            status=status,
            fps=fps,
            latency_ms=latency_ms,
            last_frame_ts=last_frame_ts,
        )

        with self._lock:
            if camera_id not in self._cameras:
                self._camera_order.append(camera_id)
            self._cameras[camera_id] = state

    def update(
        self,
        frame: np.ndarray,
        counts: dict[str, int],
    ) -> None:
        """Backward-compatible update for legacy single-camera detector."""
        with self._lock:
            first_id = self._first_camera_id()
        camera_id = first_id if first_id is not None else "cam_01"
        self.publish(camera_id=camera_id, annotated_frame=frame, counts=counts)

    def get_frame(self, camera_id: str | None = None) -> np.ndarray | None:
        """Return a copy of the latest annotated frame for a camera."""
        with self._lock:
            target_id = camera_id if camera_id is not None else self._first_camera_id()
            if target_id is None or target_id not in self._cameras:
                return None
            frame = self._cameras[target_id].annotated_frame
            return frame.copy() if frame is not None else None

    def get_raw_frame(self, camera_id: str | None = None) -> np.ndarray | None:
        """Return a copy of the latest raw unannotated frame for a camera."""
        with self._lock:
            target_id = camera_id if camera_id is not None else self._first_camera_id()
            if target_id is None or target_id not in self._cameras:
                return None
            frame = self._cameras[target_id].raw_frame
            return frame.copy() if frame is not None else None

    def get_jpeg(self, camera_id: str | None = None) -> bytes | None:
        """Return pre-encoded JPEG bytes for a camera (fast, no encoding under lock)."""
        with self._lock:
            target_id = camera_id if camera_id is not None else self._first_camera_id()
            if target_id is None or target_id not in self._cameras:
                return None
            return self._cameras[target_id].annotated_jpeg

    def get_counts(self, camera_id: str | None = None) -> dict[str, int]:
        """Return active tracked object counts for a camera."""
        with self._lock:
            target_id = camera_id if camera_id is not None else self._first_camera_id()
            if target_id is None or target_id not in self._cameras:
                return {"person": 0, "vehicle": 0, "animal": 0}
            return dict(self._cameras[target_id].counts)

    def get_status(self, camera_id: str | None = None) -> str:
        """Return lifecycle status for a camera."""
        with self._lock:
            target_id = camera_id if camera_id is not None else self._first_camera_id()
            if target_id is None or target_id not in self._cameras:
                return "OFFLINE"
            return self._cameras[target_id].status

    def get_all(self) -> dict[str, dict[str, Any]]:
        """Return a snapshot dictionary of all cameras' current state."""
        with self._lock:
            return {
                cid: self._cameras[cid].to_dict()
                for cid in self._camera_order
                if cid in self._cameras
            }

    def camera_ids(self) -> list[str]:
        """Return list of all registered camera IDs in order of registration."""
        with self._lock:
            return list(self._camera_order)
