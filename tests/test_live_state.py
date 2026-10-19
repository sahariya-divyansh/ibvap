"""Unit tests for backend.live_state per-camera store and backward compatibility."""

from __future__ import annotations

import threading
import time

import numpy as np

from backend.live_state import LiveDetectorState


def test_multi_camera_isolation() -> None:
    """Publishing to different cameras stores isolated frames and counts."""
    state = LiveDetectorState()

    frame1 = np.ones((10, 10, 3), dtype=np.uint8) * 10
    frame2 = np.ones((10, 10, 3), dtype=np.uint8) * 20

    state.publish(
        camera_id="cam_01",
        annotated_frame=frame1,
        counts={"person": 2, "vehicle": 1},
        status="LIVE",
        fps=15.0,
        latency_ms=45.0,
        last_frame_ts=100.0,
    )
    state.publish(
        camera_id="cam_02",
        annotated_frame=frame2,
        counts={"person": 0, "vehicle": 3, "animal": 1},
        status="DEGRADED",
        fps=20.0,
        latency_ms=30.0,
        last_frame_ts=101.0,
    )

    assert state.camera_ids() == ["cam_01", "cam_02"]

    f1 = state.get_frame("cam_01")
    f2 = state.get_frame("cam_02")
    assert f1 is not None and f2 is not None
    assert f1[0, 0, 0] == 10
    assert f2[0, 0, 0] == 20

    assert state.get_counts("cam_01") == {"person": 2, "vehicle": 1, "animal": 0}
    assert state.get_counts("cam_02") == {"person": 0, "vehicle": 3, "animal": 1}
    assert state.get_status("cam_01") == "LIVE"
    assert state.get_status("cam_02") == "DEGRADED"

    all_data = state.get_all()
    assert "cam_01" in all_data and "cam_02" in all_data
    assert all_data["cam_01"]["fps"] == 15.0
    assert all_data["cam_02"]["fps"] == 20.0
    assert all_data["cam_01"]["annotated_jpeg"] is not None


def test_backward_compatible_first_camera_api() -> None:
    """Parameterless get_frame() and get_counts() delegate to the first camera."""
    state = LiveDetectorState()
    assert state.get_frame() is None
    assert state.get_counts() == {"person": 0, "vehicle": 0, "animal": 0}

    frame_legacy = np.ones((8, 8, 3), dtype=np.uint8) * 55
    state.update(frame_legacy, {"person": 4})

    assert state.camera_ids() == ["cam_01"]
    f = state.get_frame()
    assert f is not None
    assert f[0, 0, 0] == 55
    assert state.get_counts() == {"person": 4, "vehicle": 0, "animal": 0}
    assert state.get_jpeg() is not None

    # Adding a second camera should not change the first camera default
    frame2 = np.ones((8, 8, 3), dtype=np.uint8) * 88
    state.publish("cam_02", frame2, {"person": 1})
    assert state.get_frame()[0, 0, 0] == 55
    assert state.get_counts()["person"] == 4


def test_thread_safety_smoke() -> None:
    """Multiple concurrent writers and readers access state without deadlocks or errors."""
    state = LiveDetectorState()
    stop_event = threading.Event()
    errors: list[Exception] = []

    def writer(cid: str, val: int) -> None:
        frame = np.ones((16, 16, 3), dtype=np.uint8) * val
        while not stop_event.is_set():
            try:
                state.publish(
                    camera_id=cid,
                    annotated_frame=frame,
                    counts={"person": val},
                    fps=30.0,
                )
                time.sleep(0.001)
            except (RuntimeError, ValueError, KeyError) as exc:
                errors.append(exc)

    def reader() -> None:
        while not stop_event.is_set():
            try:
                _ = state.get_frame()
                _ = state.get_counts()
                _ = state.get_all()
                _ = state.camera_ids()
                time.sleep(0.001)
            except (RuntimeError, ValueError, KeyError) as exc:
                errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=(f"cam_{i}", i), daemon=True)
        for i in range(4)
    ]
    threads.extend(
        threading.Thread(target=reader, daemon=True) for _ in range(4)
    )

    for t in threads:
        t.start()

    time.sleep(0.1)
    stop_event.set()

    for t in threads:
        t.join(timeout=1.0)

    assert not errors
    assert len(state.camera_ids()) == 4
