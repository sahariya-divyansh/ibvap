"""Unit and integration tests for IBVAP multi-camera REST API endpoints [D6]."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

import backend.main
from backend.camera_config import CameraConfig, FenceConfig
from backend.camera_manager import CameraManager
from backend.db import init_db, insert_event
from backend.main import app, live_state


def _make_dummy_cam(camera_id: str, location: str = "Test Location") -> CameraConfig:
    return CameraConfig(
        camera_id=camera_id,
        location=location,
        source=0,
        loop=False,
        inference_stride=1,
        target_width=640,
        fence=FenceConfig(start=[0.0, 0.5], end=[1.0, 0.5], alert_direction="ANY"),
        zone=[[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
    )


class FakeWorker:
    def __init__(self, cam: CameraConfig, *args: Any, **kwargs: Any) -> None:
        self.cam = cam
        self.update_geometry_calls: list[tuple[Any, Any]] = []
        self._current_frame_size = (640, 480)
        self.tamper_active = False

    def is_tamper_active(self) -> bool:
        return self.tamper_active

    def update_geometry(self, fence: Any, zone: Any) -> None:
        self.update_geometry_calls.append((fence, zone))


@pytest.fixture
def mock_app_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Set up fake camera manager and isolated test DB for the FastAPI test client."""
    db_file = tmp_path / "test_api.db"
    init_db(db_file)
    old_settings = backend.main.settings
    new_settings = dataclasses.replace(old_settings, database_path=db_file)
    monkeypatch.setattr(backend.main, "settings", new_settings)

    cam1 = _make_dummy_cam("cam_01", "Gate North")
    cam2 = _make_dummy_cam("cam_02", "Gate South")

    manager = CameraManager(
        cameras=[cam1, cam2],
        settings=new_settings,
        live_state=live_state,
        worker_factory=FakeWorker,
    )

    # Point IBVAP_CAMERAS_FILE to tmp file for persistence testing
    cameras_tmp = tmp_path / "cameras.json"
    monkeypatch.setenv("IBVAP_CAMERAS_FILE", str(cameras_tmp))

    app.state.camera_manager = manager

    yield manager, cameras_tmp, db_file

    app.state.camera_manager = None


def test_cameras_shape_and_degraded_status(mock_app_state) -> None:
    """GET /cameras returns correct shape and sets DEGRADED when LIVE and fps < 3."""
    client = TestClient(app)

    # Publish state for cam_01: LIVE with 1.5 fps -> should be DEGRADED
    dummy_frame = np.zeros((480, 640, 3), dtype=np.uint8)
    live_state.publish(
        camera_id="cam_01",
        annotated_frame=dummy_frame,
        counts={"person": 2, "vehicle": 1, "animal": 0},
        status="LIVE",
        fps=1.5,
        latency_ms=120.0,
        last_frame_ts=100.0,
        raw_frame=dummy_frame,
    )

    # Publish state for cam_02: LIVE with 15.0 fps -> should be LIVE
    live_state.publish(
        camera_id="cam_02",
        annotated_frame=dummy_frame,
        counts={"person": 0, "vehicle": 3, "animal": 1},
        status="LIVE",
        fps=15.0,
        latency_ms=80.0,
        last_frame_ts=100.0,
        raw_frame=dummy_frame,
    )

    resp = client.get("/cameras")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2

    cam01 = next(c for c in data if c["camera_id"] == "cam_01")
    assert cam01["status"] == "DEGRADED"
    assert cam01["location"] == "Gate North"
    assert cam01["fps"] == 1.5
    assert cam01["counts"] == {"person": 2, "vehicle": 1, "animal": 0}
    assert cam01["latency_ms"] == 120.0
    assert isinstance(cam01["last_frame_age_s"], float)
    assert cam01["low_visibility"] is False

    cam02 = next(c for c in data if c["camera_id"] == "cam_02")
    assert cam02["status"] == "LIVE"
    assert cam02["fps"] == 15.0


def test_cameras_reports_degraded_when_tamper_active(mock_app_state) -> None:
    """GET /cameras reports DEGRADED when tamper is active, even at normal FPS."""
    manager, _, _ = mock_app_state
    client = TestClient(app)

    dummy_frame = np.zeros((480, 640, 3), dtype=np.uint8)
    # cam_01 has fps=15.0 (healthy FPS) but tamper_active=True
    live_state.publish(
        camera_id="cam_01",
        annotated_frame=dummy_frame,
        counts={"person": 1, "vehicle": 0, "animal": 0},
        status="LIVE",
        fps=15.0,
        latency_ms=50.0,
        last_frame_ts=100.0,
        raw_frame=dummy_frame,
        tamper_active=True,
    )

    resp = client.get("/cameras")
    assert resp.status_code == 200
    cam01 = next(c for c in resp.json() if c["camera_id"] == "cam_01")
    assert cam01["status"] == "DEGRADED"

    # Also test via worker is_tamper_active()
    live_state.publish(
        camera_id="cam_01",
        annotated_frame=dummy_frame,
        counts={"person": 1, "vehicle": 0, "animal": 0},
        status="LIVE",
        fps=15.0,
        latency_ms=50.0,
        last_frame_ts=100.0,
        raw_frame=dummy_frame,
        tamper_active=False,
    )
    worker1 = manager.get("cam_01")
    worker1.tamper_active = True
    resp = client.get("/cameras")
    assert resp.status_code == 200
    cam01 = next(c for c in resp.json() if c["camera_id"] == "cam_01")
    assert cam01["status"] == "DEGRADED"

    # Clear tamper
    worker1.tamper_active = False
    resp = client.get("/cameras")
    assert resp.status_code == 200
    cam01 = next(c for c in resp.json() if c["camera_id"] == "cam_01")
    assert cam01["status"] == "LIVE"


def test_unknown_camera_returns_404(mock_app_state) -> None:
    """Endpoints return 404 for non-existent camera IDs."""
    client = TestClient(app)
    bad_id = "non_existent_cam"

    assert client.get(f"/video_feed/{bad_id}").status_code == 404
    assert client.get(f"/cameras/{bad_id}/snapshot").status_code == 404
    assert client.get(f"/cameras/{bad_id}/config").status_code == 404
    assert client.get(f"/stats?camera_id={bad_id}").status_code == 404

    put_body = {
        "fence": {"start": [0.0, 0.5], "end": [1.0, 0.5], "alert_direction": "ANY"},
        "zone": [],
    }
    assert client.put(f"/cameras/{bad_id}/config", json=put_body).status_code == 404


def test_snapshot_503_when_no_frame(mock_app_state) -> None:
    """GET /cameras/{camera_id}/snapshot returns 503 when no raw frame has arrived yet."""
    client = TestClient(app)
    # Clear live state for cam_01
    with live_state._lock:
        live_state._cameras.pop("cam_01", None)

    resp = client.get("/cameras/cam_01/snapshot")
    assert resp.status_code == 503


def test_snapshot_success_when_frame_available(mock_app_state) -> None:
    """GET /cameras/{camera_id}/snapshot returns image/jpeg when raw frame exists."""
    client = TestClient(app)
    raw = np.full((100, 100, 3), 128, dtype=np.uint8)
    live_state.publish(
        camera_id="cam_01",
        annotated_frame=raw,
        counts={"person": 0, "vehicle": 0, "animal": 0},
        raw_frame=raw,
    )

    resp = client.get("/cameras/cam_01/snapshot")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert len(resp.content) > 0


def test_put_config_validation_failures(mock_app_state) -> None:
    """PUT /cameras/{camera_id}/config returns 422 for invalid payloads."""
    client = TestClient(app)

    # 1. 1-point fence
    r1 = client.put(
        "/cameras/cam_01/config",
        json={"fence": {"start": [0.5], "end": [1.0, 0.5]}, "zone": []},
    )
    assert r1.status_code == 422

    # 2. 2-point zone
    r2 = client.put(
        "/cameras/cam_01/config",
        json={
            "fence": {"start": [0.0, 0.5], "end": [1.0, 0.5]},
            "zone": [[0.1, 0.1], [0.5, 0.5]],
        },
    )
    assert r2.status_code == 422

    # 3. Coordinate > 1.0 (e.g. 1.5)
    r3 = client.put(
        "/cameras/cam_01/config",
        json={"fence": {"start": [1.5, 0.5], "end": [1.0, 0.5]}, "zone": []},
    )
    assert r3.status_code == 422

    # 4. Negative coordinate
    r4 = client.put(
        "/cameras/cam_01/config",
        json={"fence": {"start": [-0.1, 0.5], "end": [1.0, 0.5]}, "zone": []},
    )
    assert r4.status_code == 422

    # 5. Bad alert_direction
    r5 = client.put(
        "/cameras/cam_01/config",
        json={
            "fence": {"start": [0.0, 0.5], "end": [1.0, 0.5], "alert_direction": "INVALID_DIR"},
            "zone": [],
        },
    )
    assert r5.status_code == 422


def test_put_config_success_hot_reloads_and_persists(mock_app_state) -> None:
    """Valid PUT updates geometry, calls worker.update_geometry once, and writes file atomically."""
    manager, cameras_tmp, _ = mock_app_state
    client = TestClient(app)

    valid_payload = {
        "fence": {
            "start": [0.1, 0.2],
            "end": [0.8, 0.9],
            "alert_direction": "A_TO_B",
        },
        "zone": [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]],
    }

    resp = client.put("/cameras/cam_01/config", json=valid_payload)
    assert resp.status_code == 200
    res_data = resp.json()
    assert res_data["fence"]["start"] == [0.1, 0.2]
    assert res_data["fence"]["alert_direction"] == "A_TO_B"
    assert len(res_data["zone"]) == 4

    # Worker had update_geometry called once
    worker = manager.get("cam_01")
    assert isinstance(worker, FakeWorker)
    assert len(worker.update_geometry_calls) == 1
    fence_arg, zone_arg = worker.update_geometry_calls[0]
    assert fence_arg.start == [0.1, 0.2]
    assert len(zone_arg) == 4

    # File was saved to disk
    assert cameras_tmp.exists()
    content = cameras_tmp.read_text(encoding="utf-8")
    assert "A_TO_B" in content
    assert "cam_01" in content


def test_events_query_filters_and_evidence_url(mock_app_state) -> None:
    """GET /events filters by severity and ack_status; returns derived evidence_url."""
    _, _, db_file = mock_app_state
    client = TestClient(app)

    insert_event(
        {
            "event_id": "ev_high_pending",
            "camera_id": "cam_01",
            "timestamp": "2026-10-18T10:00:00Z",
            "event_type": "VIRTUAL_FENCE_CROSS",
            "object_type": "person",
            "track_id": 10,
            "confidence": 0.9,
            "evidence_path": "evidence/snap_high.jpg",
            "severity": "HIGH",
            "ack_status": "pending",
        },
        db_path=db_file,
    )
    insert_event(
        {
            "event_id": "ev_low_confirmed",
            "camera_id": "cam_01",
            "timestamp": "2026-10-18T10:01:00Z",
            "event_type": "NEW_OBJECT_DETECTED",
            "object_type": "animal",
            "track_id": 11,
            "confidence": 0.8,
            "evidence_path": "",
            "severity": "LOW",
            "ack_status": "confirmed",
        },
        db_path=db_file,
    )

    # 1. Filter severity=HIGH
    r_high = client.get("/events?severity=HIGH")
    assert r_high.status_code == 200
    events_high = r_high.json()
    assert len(events_high) == 1
    assert events_high[0]["event_id"] == "ev_high_pending"
    assert events_high[0]["evidence_url"] == "/evidence/snap_high.jpg"

    # 2. Filter ack_status=confirmed
    r_conf = client.get("/events?ack_status=confirmed")
    assert r_conf.status_code == 200
    events_conf = r_conf.json()
    assert len(events_conf) == 1
    assert events_conf[0]["event_id"] == "ev_low_confirmed"
    assert events_conf[0]["evidence_url"] is None

    # 3. Invalid filters -> 422
    assert client.get("/events?severity=INVALID").status_code == 422
    assert client.get("/events?ack_status=INVALID").status_code == 422


def test_stats_per_camera_and_sum(mock_app_state) -> None:
    """GET /stats calculates per-camera counts or the sum across all cameras."""
    client = TestClient(app)
    dummy = np.zeros((10, 10, 3), dtype=np.uint8)

    live_state.publish(
        camera_id="cam_01",
        annotated_frame=dummy,
        counts={"person": 3, "vehicle": 2, "animal": 1},
    )
    live_state.publish(
        camera_id="cam_02",
        annotated_frame=dummy,
        counts={"person": 1, "vehicle": 4, "animal": 0},
    )

    # Specific camera
    r_cam01 = client.get("/stats?camera_id=cam_01")
    assert r_cam01.status_code == 200
    assert r_cam01.json() == {
        "person": 3,
        "vehicle": 2,
        "animal": 1,
        "suppressed_animal_events": 0,
    }

    # Sum across all cameras
    r_sum = client.get("/stats")
    assert r_sum.status_code == 200
    assert r_sum.json() == {
        "person": 4,
        "vehicle": 6,
        "animal": 1,
        "suppressed_animal_events": 0,
    }
