"""FastAPI entrypoint for the IBVAP backend."""

from __future__ import annotations

import logging
import math
import threading
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

import cv2
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from backend.camera_config import (
    CameraConfigError,
    FenceConfig,
    load_cameras,
    save_cameras,
)
from backend.camera_manager import CameraManager
from backend.config import Settings, get_settings
from backend.db import get_event, init_db, list_events, update_event_ack
from backend.detector import Detector
from backend.live_state import LiveDetectorState

LOGGER = logging.getLogger(__name__)

settings = get_settings()
settings.evidence_dir.mkdir(parents=True, exist_ok=True)
detector_stop_event = threading.Event()
detector_thread: threading.Thread | None = None
detector_lock = threading.Lock()
live_state = LiveDetectorState()
camera_manager: CameraManager | None = None
_CONFIG_LOCK = threading.Lock()


class AckRequest(BaseModel):
    """Human review status for an event."""

    status: str


class FenceConfigUpdate(BaseModel):
    """Payload validation for virtual fence update."""

    model_config = ConfigDict(extra="forbid")

    start: list[float]
    end: list[float]
    alert_direction: Literal["ANY", "A_TO_B", "B_TO_A"] = "ANY"

    @field_validator("start", "end")
    @classmethod
    def validate_coord(cls, v: list[float], info: ValidationInfo) -> list[float]:
        if len(v) != 2:
            raise ValueError(f"'{info.field_name}' must have exactly 2 coordinates [x, y], got {len(v)}")
        for idx, val in enumerate(v):
            if not isinstance(val, (int, float)) or not math.isfinite(val) or not (0.0 <= val <= 1.0):
                raise ValueError(
                    f"Coordinate {idx} in '{info.field_name}' must be a finite number within [0, 1], got {val}"
                )
        return v


class CameraConfigUpdate(BaseModel):
    """Payload validation for camera geometry update."""

    model_config = ConfigDict(extra="forbid")

    fence: FenceConfigUpdate
    zone: list[list[float]] = Field(default_factory=list)

    @field_validator("zone")
    @classmethod
    def validate_zone(cls, v: list[list[float]]) -> list[list[float]]:
        if len(v) != 0 and len(v) < 3:
            raise ValueError(f"Zone must be empty or contain at least 3 points, got {len(v)}")
        for idx, pt in enumerate(v):
            if len(pt) != 2:
                raise ValueError(f"Zone point at index {idx} must have exactly 2 coordinates [x, y], got {len(pt)}")
            for c_idx, val in enumerate(pt):
                if not isinstance(val, (int, float)) or not math.isfinite(val) or not (0.0 <= val <= 1.0):
                    raise ValueError(
                        f"Zone point at index {idx} coordinate {c_idx} must be a finite number within [0, 1], got {val}"
                    )
        return v


def get_camera_manager() -> CameraManager:
    """Retrieve the camera manager, supporting test injection via app.state."""
    if hasattr(app.state, "camera_manager") and app.state.camera_manager is not None:
        return app.state.camera_manager
    if camera_manager is not None:
        return camera_manager
    raise HTTPException(status_code=503, detail="Camera manager not initialized")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize storage and start/stop camera workers."""
    global camera_manager
    init_db(settings.database_path)
    settings.evidence_dir.mkdir(parents=True, exist_ok=True)

    # Allow tests to inject a camera manager before startup to bypass real hardware/YOLO
    if getattr(app.state, "camera_manager", None) is not None:
        camera_manager = app.state.camera_manager
        yield
        return

    try:
        cameras = load_cameras()
    except CameraConfigError as exc:
        LOGGER.critical("Invalid camera configuration: %s", exc)
        raise RuntimeError(f"Invalid camera configuration: {exc}") from exc

    camera_manager = CameraManager(
        cameras=cameras,
        settings=settings,
        live_state=live_state,
    )
    camera_manager.start_all()
    yield
    if camera_manager is not None:
        camera_manager.stop_all()
    detector_stop_event.set()


app = FastAPI(title="IBVAP Backend", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/evidence", StaticFiles(directory=settings.evidence_dir), name="evidence")


@app.get("/health")
def health() -> dict[str, str]:
    """Return API health status."""
    return {"status": "ok"}


@app.get("/cameras")
def list_cameras() -> list[dict[str, Any]]:
    """Return list of all registered cameras and their current statuses and metrics."""
    manager = get_camera_manager()
    now = time.monotonic()
    result = []
    for cam in manager.cameras:
        cid = cam.camera_id
        raw_status = live_state.get_status(cid)
        counts = live_state.get_counts(cid)

        with live_state._lock:
            state = live_state._cameras.get(cid)
            fps = round(state.fps, 1) if state else 0.0
            latency_ms = (
                round(state.latency_ms, 1)
                if state and state.last_frame_ts is not None and state.latency_ms > 0
                else None
            )
            last_frame_age_s = (
                max(0.0, round(now - state.last_frame_ts, 2))
                if state and state.last_frame_ts is not None
                else None
            )

        if raw_status == "LIVE" and fps < 3.0:
            status = "DEGRADED"
        else:
            status = raw_status

        result.append(
            {
                "camera_id": cid,
                "location": cam.location,
                "status": status,
                "fps": fps,
                "latency_ms": latency_ms,
                "last_frame_age_s": last_frame_age_s,
                "low_visibility": False,
                "counts": {
                    "person": counts.get("person", 0),
                    "vehicle": counts.get("vehicle", 0),
                    "animal": counts.get("animal", 0),
                },
            }
        )
    return result


@app.get("/stats")
def read_stats(camera_id: str | None = None) -> dict[str, int]:
    """Return current-frame active tracked object counts."""
    manager = get_camera_manager()
    if camera_id is not None:
        if camera_id not in manager.ids():
            raise HTTPException(status_code=404, detail=f"Camera '{camera_id}' not found")
        counts = live_state.get_counts(camera_id)
        return {
            "person": counts.get("person", 0),
            "vehicle": counts.get("vehicle", 0),
            "animal": counts.get("animal", 0),
            "suppressed_animal_events": 0,
        }

    total = {"person": 0, "vehicle": 0, "animal": 0, "suppressed_animal_events": 0}
    for cid in manager.ids():
        c = live_state.get_counts(cid)
        total["person"] += c.get("person", 0)
        total["vehicle"] += c.get("vehicle", 0)
        total["animal"] += c.get("animal", 0)
    return total


@app.get("/video_feed")
def video_feed() -> StreamingResponse:
    """Stream latest annotated detector frames as multipart MJPEG for first camera."""
    return StreamingResponse(
        generate_mjpeg_frames(None),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/video_feed/{camera_id}")
def camera_video_feed(camera_id: str) -> StreamingResponse:
    """Stream latest annotated detector frames as multipart MJPEG for a specific camera."""
    manager = get_camera_manager()
    if camera_id not in manager.ids():
        raise HTTPException(status_code=404, detail=f"Camera '{camera_id}' not found")
    return StreamingResponse(
        generate_mjpeg_frames(camera_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/cameras/{camera_id}/snapshot")
def camera_snapshot(camera_id: str) -> Response:
    """Return a single JPEG snapshot of the latest raw frame."""
    manager = get_camera_manager()
    if camera_id not in manager.ids():
        raise HTTPException(status_code=404, detail=f"Camera '{camera_id}' not found")

    raw_frame = live_state.get_raw_frame(camera_id)
    if raw_frame is None:
        raise HTTPException(status_code=503, detail="No snapshot available for camera")

    success, encoded = cv2.imencode(".jpg", raw_frame)
    if not success:
        raise HTTPException(status_code=503, detail="Failed to encode snapshot")

    return Response(content=encoded.tobytes(), media_type="image/jpeg")


def _normalize_pt(pt: list[float], frame_size: list[int] | None) -> list[float]:
    """Convert coordinate point to normalized space in [0, 1] if frame size is known."""
    if frame_size and (pt[0] > 1.0 or pt[1] > 1.0):
        w, h = frame_size
        return [max(0.0, min(1.0, pt[0] / w)), max(0.0, min(1.0, pt[1] / h))]
    return [float(pt[0]), float(pt[1])]


@app.get("/cameras/{camera_id}/config")
def get_camera_config(camera_id: str) -> dict[str, Any]:
    """Return normalized fence and zone geometry for a camera."""
    manager = get_camera_manager()
    cam = next((c for c in manager.cameras if c.camera_id == camera_id), None)
    if cam is None:
        raise HTTPException(status_code=404, detail=f"Camera '{camera_id}' not found")

    frame_size = None
    raw_frame = live_state.get_raw_frame(camera_id)
    if raw_frame is not None:
        h, w = raw_frame.shape[:2]
        frame_size = [w, h]
    else:
        worker = manager.get(camera_id)
        if worker and getattr(worker, "_current_frame_size", None):
            frame_size = list(worker._current_frame_size)

    return {
        "fence": {
            "start": _normalize_pt(cam.fence.start, frame_size),
            "end": _normalize_pt(cam.fence.end, frame_size),
            "alert_direction": cam.fence.alert_direction,
        },
        "zone": [_normalize_pt(pt, frame_size) for pt in cam.zone],
        "frame_size": frame_size,
    }


@app.put("/cameras/{camera_id}/config")
def update_camera_config(
    camera_id: str,
    body: CameraConfigUpdate,
) -> dict[str, Any]:
    """Update fence and zone coordinates, hot-reloading worker and saving cameras file."""
    manager = get_camera_manager()
    target_idx = None
    for idx, c in enumerate(manager.cameras):
        if c.camera_id == camera_id:
            target_idx = idx
            break

    if target_idx is None:
        raise HTTPException(status_code=404, detail=f"Camera '{camera_id}' not found")

    new_fence = FenceConfig(
        start=body.fence.start,
        end=body.fence.end,
        alert_direction=body.fence.alert_direction,
    )
    new_zone = [[pt[0], pt[1]] for pt in body.zone]

    with _CONFIG_LOCK:
        # Update in-memory CameraConfig
        manager.cameras[target_idx] = manager.cameras[target_idx].model_copy(
            update={"fence": new_fence, "zone": new_zone}
        )

        # Hot-reload the running worker
        worker = manager.get(camera_id)
        if worker and hasattr(worker, "update_geometry"):
            worker.update_geometry(new_fence, new_zone)

        # Atomic persist to cameras.json
        save_cameras(manager.cameras)

    frame_size = None
    raw_frame = live_state.get_raw_frame(camera_id)
    if raw_frame is not None:
        h, w = raw_frame.shape[:2]
        frame_size = [w, h]
    else:
        if worker and getattr(worker, "_current_frame_size", None):
            frame_size = list(worker._current_frame_size)

    return {
        "fence": {
            "start": body.fence.start,
            "end": body.fence.end,
            "alert_direction": body.fence.alert_direction,
        },
        "zone": new_zone,
        "frame_size": frame_size,
    }


@app.get("/events")
def read_events(
    camera_id: str | None = None,
    event_type: str | None = None,
    severity: str | None = None,
    ack_status: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[dict[str, Any]]:
    """Return stored events newest first with optional query filters."""
    if severity is not None and severity not in {"LOW", "MEDIUM", "HIGH"}:
        raise HTTPException(
            status_code=422,
            detail="Invalid severity filter: must be LOW, MEDIUM, or HIGH",
        )
    if ack_status is not None and ack_status not in {"pending", "confirmed", "rejected"}:
        raise HTTPException(
            status_code=422,
            detail="Invalid ack_status filter: must be pending, confirmed, or rejected",
        )

    return list_events(
        camera_id=camera_id,
        event_type=event_type,
        severity=severity,
        ack_status=ack_status,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        db_path=settings.database_path,
    )


@app.get("/events/{event_id}")
def read_event(event_id: str) -> dict[str, Any]:
    """Return one stored event by ID."""
    event = get_event(event_id, settings.database_path)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


@app.post("/events/{event_id}/ack")
def acknowledge_event(event_id: str, request: AckRequest) -> dict[str, Any]:
    """Confirm or reject an event after human review."""
    if request.status not in {"confirmed", "rejected"}:
        raise HTTPException(status_code=400, detail="Invalid ack status")

    event = update_event_ack(event_id, request.status, settings.database_path)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


def start_detector_thread(settings: Settings) -> None:
    """Start the blocking detector loop in a background thread."""
    global detector_thread
    with detector_lock:
        if detector_thread is not None and detector_thread.is_alive():
            return
        detector_stop_event.clear()
        detector_thread = threading.Thread(
            target=run_detector,
            args=(settings, detector_stop_event),
            name="ibvap-detector",
            daemon=True,
        )
        detector_thread.start()
        LOGGER.info("Detector thread started")


def run_detector(settings: Settings, stop_event: threading.Event) -> None:
    """Run detector worker and keep API alive if camera startup fails."""
    try:
        Detector(settings, stop_event=stop_event, live_state=live_state).run()
    except (RuntimeError, ValueError) as exc:
        LOGGER.error("Detector worker stopped: %s", exc)


def generate_mjpeg_frames(camera_id: str | None = None):
    """Yield JPEG-encoded latest frames for MJPEG clients with rate limiting."""
    frame_interval = 1.0 / 15.0
    while not detector_stop_event.is_set():
        jpeg_bytes = live_state.get_jpeg(camera_id)
        if jpeg_bytes is not None:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + jpeg_bytes
                + b"\r\n"
            )
            time.sleep(frame_interval)
            continue

        frame = live_state.get_frame(camera_id)
        if frame is not None:
            success, encoded = cv2.imencode(".jpg", frame)
            if success:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n"
                    + encoded.tobytes()
                    + b"\r\n"
                )
                time.sleep(frame_interval)
                continue

        time.sleep(frame_interval)
