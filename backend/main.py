"""FastAPI entrypoint for the IBVAP backend."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any

import cv2
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.camera_config import CameraConfigError, load_cameras
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


class AckRequest(BaseModel):
    """Human review status for an event."""

    status: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize storage and start/stop camera workers."""
    global camera_manager
    init_db(settings.database_path)
    settings.evidence_dir.mkdir(parents=True, exist_ok=True)

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


@app.get("/stats")
def read_stats() -> dict[str, int]:
    """Return current-frame active tracked object counts."""
    return live_state.get_counts()


@app.get("/video_feed")
def video_feed() -> StreamingResponse:
    """Stream latest annotated detector frames as multipart MJPEG."""
    return StreamingResponse(
        generate_mjpeg_frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/events")
def read_events(
    camera_id: str | None = None,
    event_type: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[dict[str, Any]]:
    """Return stored events newest first."""
    return list_events(
        camera_id=camera_id,
        event_type=event_type,
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


def generate_mjpeg_frames():
    """Yield JPEG-encoded latest frames for MJPEG clients."""
    while not detector_stop_event.is_set():
        jpeg_bytes = live_state.get_jpeg()
        if jpeg_bytes is not None:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + jpeg_bytes
                + b"\r\n"
            )
            time.sleep(0.04)
            continue

        frame = live_state.get_frame()
        if frame is None:
            time.sleep(0.1)
            continue

        success, encoded = cv2.imencode(".jpg", frame)
        if not success:
            time.sleep(0.1)
            continue

        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n"
            + encoded.tobytes()
            + b"\r\n"
        )
        time.sleep(0.04)
