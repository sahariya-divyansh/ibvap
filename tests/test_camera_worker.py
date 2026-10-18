"""Unit tests for backend.camera_worker with fakes (no real models, cameras, or sleeping)."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import Any

import numpy as np

from backend.camera_config import CameraConfig, FenceConfig
from backend.camera_worker import CameraWorker
from backend.config import Settings
from backend.live_state import LiveDetectorState
from backend.stream_reader import StreamStatus


def _wait_until(predicate: Any, timeout: float = 2.0, interval: float = 0.005) -> bool:
    """Poll predicate until True or timeout expires."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class FakeBox:
    """Mock YOLO detection box."""

    def __init__(
        self,
        track_id: int,
        xyxy: list[int],
        class_id: int = 0,
        conf: float = 0.9,
    ) -> None:
        self.id = [track_id]
        self.xyxy = [np.array(xyxy, dtype=np.int32)]
        self.cls = [class_id]
        self.conf = [conf]


class FakeResult:
    """Mock YOLO tracking result object."""

    def __init__(self, boxes: list[FakeBox] | None = None) -> None:
        self.boxes = boxes or []
        self.names = {0: "person", 2: "car"}


class FakeModel:
    """Mock YOLO model returning scripted results."""

    def __init__(self, script: list[FakeResult] | None = None, raise_error: bool = False) -> None:
        self.script = list(script) if script is not None else []
        self.call_count = 0
        self.raise_error = raise_error

    def track(self, frame: np.ndarray, **kwargs: Any) -> list[FakeResult]:
        self.call_count += 1
        if self.raise_error:
            raise RuntimeError("Scripted model failure")
        if self.script:
            idx = min(self.call_count - 1, len(self.script) - 1)
            return [self.script[idx]]
        return [FakeResult()]


class FakeReader:
    """Mock StreamReader providing controlled frames and status callbacks."""

    def __init__(
        self,
        frames: list[tuple[np.ndarray, float]] | None = None,
        on_status_change: Any | None = None,
        **kwargs: Any,
    ) -> None:
        self.frames = list(frames) if frames is not None else []
        self.idx = 0
        self.on_status_change = on_status_change
        self.status = StreamStatus.LIVE
        self.fps = 15.0
        self.stopped = False

    def latest(self) -> tuple[np.ndarray, float] | None:
        if self.stopped or not self.frames:
            return None
        if self.idx < len(self.frames):
            frame, ts = self.frames[self.idx]
            self.idx += 1
            return frame, ts
        return self.frames[-1]

    def start(self) -> None:
        pass

    def stop(self, timeout: float = 2.0) -> None:
        self.stopped = True


def _make_dummy_settings() -> Settings:
    from backend.config import get_settings

    return replace(get_settings(), display_enabled=False)


def _make_dummy_cam(camera_id: str = "cam_01", **kwargs: Any) -> CameraConfig:
    params: dict[str, Any] = {
        "camera_id": camera_id,
        "location": "Gate",
        "source": 0,
        "fence": FenceConfig(start=[0.0, 0.5], end=[1.0, 0.5]),
        "zone": [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]],
    }
    params.update(kwargs)
    return CameraConfig(**params)


def test_stride_processes_every_nth_frame_and_publishes_all() -> None:
    """stride=2 runs inference every 2nd frame but publishes every frame."""
    f1 = np.ones((10, 10, 3), dtype=np.uint8) * 1
    f2 = np.ones((10, 10, 3), dtype=np.uint8) * 2
    f3 = np.ones((10, 10, 3), dtype=np.uint8) * 3
    f4 = np.ones((10, 10, 3), dtype=np.uint8) * 4
    frames = [(f1, 1.0), (f2, 2.0), (f3, 3.0), (f4, 4.0)]

    model = FakeModel()
    live_state = LiveDetectorState()
    cam = _make_dummy_cam(inference_stride=2)

    worker = CameraWorker(
        cam=cam,
        settings=_make_dummy_settings(),
        live_state=live_state,
        reader_factory=lambda **kw: FakeReader(frames=frames, **kw),
        model_factory=lambda: model,
    )
    worker.start()

    try:
        assert _wait_until(
            lambda: live_state.get_all().get("cam_01", {}).get("last_frame_ts") == 4.0
        )
        assert model.call_count == 2
        assert live_state.get_frame("cam_01") is not None
    finally:
        worker.stop()


def test_geometry_resolved_from_frame_size() -> None:
    """Worker resolves normalized fence/zone coordinates based on frame dimensions."""
    frame = np.ones((480, 640, 3), dtype=np.uint8)
    cam = _make_dummy_cam(
        fence=FenceConfig(start=[0.0, 0.5], end=[1.0, 0.5]),
        zone=[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]],
    )
    worker = CameraWorker(
        cam=cam,
        settings=_make_dummy_settings(),
        reader_factory=lambda **kw: FakeReader(frames=[(frame, 1.0)], **kw),
        model_factory=lambda: FakeModel(),
    )
    worker.start()

    try:
        assert _wait_until(lambda: worker._current_frame_size == (640, 480))
        fem = worker._fence_event_manager
        assert fem is not None
        assert fem.fence.start == (0.0, 240.0)
        assert fem.fence.end == (640.0, 240.0)
    finally:
        worker.stop()


def test_fence_crossing_produces_event_with_camera_id() -> None:
    """Crossing fence produces exactly one event carrying the camera's ID."""
    events_recorded: list[dict[str, Any]] = []

    box1 = FakeBox(track_id=1, xyxy=[310, 90, 330, 110])
    box2 = FakeBox(track_id=1, xyxy=[310, 290, 330, 310])
    box3 = FakeBox(track_id=1, xyxy=[310, 340, 330, 360])

    frames = [
        (np.ones((480, 640, 3), dtype=np.uint8), 1.0),
        (np.ones((480, 640, 3), dtype=np.uint8), 2.0),
        (np.ones((480, 640, 3), dtype=np.uint8), 3.0),
    ]
    model = FakeModel(script=[FakeResult([box1]), FakeResult([box2]), FakeResult([box3])])
    cam = _make_dummy_cam(camera_id="cam_gate_west")

    worker = CameraWorker(
        cam=cam,
        settings=_make_dummy_settings(),
        reader_factory=lambda **kw: FakeReader(frames=frames, **kw),
        model_factory=lambda: model,
        event_sink=lambda ev, **_kw: events_recorded.append(ev),
    )
    worker.start()

    try:
        assert _wait_until(
            lambda: any(e["event_type"] == "VIRTUAL_FENCE_CROSS" for e in events_recorded)
        )
        crossing_events = [e for e in events_recorded if e["event_type"] == "VIRTUAL_FENCE_CROSS"]
        assert len(crossing_events) == 1
        assert crossing_events[0]["camera_id"] == "cam_gate_west"
    finally:
        worker.stop()


def test_two_workers_do_not_share_cooldown_state() -> None:
    """Two camera workers with different geometry maintain isolated cooldown states."""
    events_cam1: list[dict[str, Any]] = []
    events_cam2: list[dict[str, Any]] = []

    # w1 fence is at y=144 (0.3 * 480); w2 fence is at y=288 (0.6 * 480)
    # Frame 1: person at y=50 (above both fences)
    box_f1 = FakeBox(track_id=1, xyxy=[310, 40, 330, 60])
    # Frame 2: person at y=200 (crosses w1 fence, above w2 fence)
    box_f2 = FakeBox(track_id=1, xyxy=[310, 190, 330, 210])
    # Frame 3: person at y=350 (same side for w1, crosses w2 fence)
    box_f3 = FakeBox(track_id=1, xyxy=[310, 340, 330, 360])

    frames = [
        (np.ones((480, 640, 3), dtype=np.uint8), 1.0),
        (np.ones((480, 640, 3), dtype=np.uint8), 2.0),
        (np.ones((480, 640, 3), dtype=np.uint8), 3.0),
    ]

    m1 = FakeModel(script=[FakeResult([box_f1]), FakeResult([box_f2]), FakeResult([box_f3])])
    m2 = FakeModel(script=[FakeResult([box_f1]), FakeResult([box_f2]), FakeResult([box_f3])])

    cam1 = _make_dummy_cam("cam_01", fence=FenceConfig(start=[0.0, 0.3], end=[1.0, 0.3]))
    cam2 = _make_dummy_cam("cam_02", fence=FenceConfig(start=[0.0, 0.6], end=[1.0, 0.6]))

    w1 = CameraWorker(
        cam=cam1,
        settings=_make_dummy_settings(),
        reader_factory=lambda **kw: FakeReader(frames=frames, **kw),
        model_factory=lambda: m1,
        event_sink=lambda ev, **_kw: events_cam1.append(ev),
    )
    w2 = CameraWorker(
        cam=cam2,
        settings=_make_dummy_settings(),
        reader_factory=lambda **kw: FakeReader(frames=frames, **kw),
        model_factory=lambda: m2,
        event_sink=lambda ev, **_kw: events_cam2.append(ev),
    )

    w1.start()
    w2.start()

    try:
        assert _wait_until(
            lambda: any(e["event_type"] == "VIRTUAL_FENCE_CROSS" for e in events_cam1)
        )
        assert _wait_until(
            lambda: any(e["event_type"] == "VIRTUAL_FENCE_CROSS" for e in events_cam2)
        )
        # Both workers emitted crossing events for track_id 1
        assert len([e for e in events_cam1 if e["event_type"] == "VIRTUAL_FENCE_CROSS"]) == 1
        assert len([e for e in events_cam2 if e["event_type"] == "VIRTUAL_FENCE_CROSS"]) == 1
    finally:
        w1.stop()
        w2.stop()


def test_consecutive_exceptions_trigger_backoff_and_recreate_model() -> None:
    """5 consecutive processing exceptions trigger backoff and model recreation while another worker runs."""
    model_creations = 0
    recreated_event = threading.Event()

    def crashing_model_factory() -> FakeModel:
        nonlocal model_creations
        model_creations += 1
        if model_creations >= 2:
            recreated_event.set()
        return FakeModel(raise_error=True)

    frame = np.ones((20, 20, 3), dtype=np.uint8)
    frames = [(frame, float(i)) for i in range(1, 20)]

    w1 = CameraWorker(
        cam=_make_dummy_cam("failing_cam"),
        settings=_make_dummy_settings(),
        reader_factory=lambda **kw: FakeReader(frames=frames, **kw),
        model_factory=crashing_model_factory,
        sleep=lambda _d: None,
    )

    live_state = LiveDetectorState()
    w2 = CameraWorker(
        cam=_make_dummy_cam("healthy_cam"),
        settings=_make_dummy_settings(),
        live_state=live_state,
        reader_factory=lambda **kw: FakeReader(frames=[(frame, 1.0)], **kw),
        model_factory=lambda: FakeModel(),
    )

    w1.start()
    w2.start()

    try:
        assert recreated_event.wait(timeout=2.0)
        assert model_creations >= 2
        assert _wait_until(lambda: live_state.get_frame("healthy_cam") is not None)
    finally:
        w1.stop()
        w2.stop()


def test_stop_is_idempotent_and_joins() -> None:
    """stop() cleanly terminates the worker thread and can be called repeatedly."""
    frame = np.ones((10, 10, 3), dtype=np.uint8)
    worker = CameraWorker(
        cam=_make_dummy_cam(),
        settings=_make_dummy_settings(),
        reader_factory=lambda **kw: FakeReader(frames=[(frame, 1.0)], **kw),
        model_factory=lambda: FakeModel(),
    )
    worker.start()

    start = time.monotonic()
    worker.stop(timeout=2.0)
    assert time.monotonic() - start < 2.0

    worker.stop()
    worker.stop()


def test_offline_online_status_events_produced() -> None:
    """Reader status changes emit exactly one CAMERA_OFFLINE and CAMERA_ONLINE event."""
    events: list[dict[str, Any]] = []
    worker = CameraWorker(
        cam=_make_dummy_cam("cam_status_test"),
        settings=_make_dummy_settings(),
        reader_factory=lambda **kw: FakeReader(**kw),
        model_factory=lambda: FakeModel(),
        event_sink=lambda ev, **_kw: events.append(ev),
    )

    worker._on_status_change(StreamStatus.LIVE, StreamStatus.OFFLINE)
    assert len(events) == 1
    ev_offline = events[0]
    assert ev_offline["event_type"] == "CAMERA_OFFLINE"
    assert ev_offline["camera_id"] == "cam_status_test"
    assert ev_offline["object_type"] == "camera"
    assert ev_offline["track_id"] == 0
    assert ev_offline["evidence_path"] == ""

    worker._on_status_change(StreamStatus.OFFLINE, StreamStatus.LIVE)
    assert len(events) == 2
    ev_online = events[1]
    assert ev_online["event_type"] == "CAMERA_ONLINE"
    assert ev_online["camera_id"] == "cam_status_test"
    assert ev_online["object_type"] == "camera"
