"""Per-camera processing worker running inference, geometry, and live state publishing."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import cv2
import numpy as np

from backend import config
from backend.ai_enricher import WHITELISTED_EVENT_TYPES
from backend.analytics.health import FrameHealthMonitor
from backend.analytics.severity import assign_severity
from backend.analytics.types import EventCandidate, EventType, Severity
from backend.camera_config import CameraConfig, FenceConfig, resolve_geometry
from backend.config import Settings
from backend.db import insert_event, update_event_ai
from backend.events import (
    Fence,
    FenceEventManager,
    RestrictedZone,
    RestrictedZoneEventManager,
    TrackedDetection,
    build_new_object_event,
)
from backend.fallback_summary import rule_based_analysis
from backend.live_state import LiveDetectorState
from backend.stream_reader import StreamReader, StreamStatus
from backend.utils.annotation import (
    draw_counts_overlay,
    draw_detections,
    draw_fence,
    draw_restricted_zone,
)
from backend.utils.detections import extract_tracked_detections
from backend.utils.evidence import save_evidence_snapshot

LOGGER = logging.getLogger(__name__)


class CameraWorker:
    """Dedicated per-camera processing pipeline owning a reader and YOLO model."""

    def __init__(
        self,
        cam: CameraConfig,
        settings: Settings,
        *,
        live_state: LiveDetectorState | None = None,
        reader_factory: Callable[..., Any] = StreamReader,
        model_factory: Callable[[], Any] | None = None,
        event_sink: Callable[..., None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        is_single_camera: bool = True,
        enricher: Any | None = None,
    ) -> None:
        """Initialize camera worker with configuration and dependencies."""
        self.cam = cam
        self.settings = settings
        self.live_state = live_state
        self.enricher = enricher
        self._model_factory = model_factory or self._default_model_factory
        self._event_sink = event_sink or self._default_event_sink
        self._clock = clock
        self._sleep = sleep
        self.is_single_camera = is_single_camera

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False

        self._model: Any = None
        self.seen_track_ids: set[int] = set()

        # Geometry and event managers protected by self._geom_lock
        self._geom_lock = threading.Lock()
        self._current_frame_size: tuple[int, int] | None = None
        self._fence_event_manager: FenceEventManager | None = None
        self._zone_event_manager: RestrictedZoneEventManager | None = None

        # Underlying video stream reader
        self.reader = reader_factory(
            source=self.cam.source,
            loop=self.cam.loop,
            clock=self._clock,
            sleep=self._sleep,
            on_status_change=self._on_status_change,
        )

        # Health monitoring and tamper status
        self._health_monitor: FrameHealthMonitor | None = None
        if self.cam.analytics.health:
            self._health_monitor = FrameHealthMonitor()
        self._health_disabled_until: float = 0.0
        self._last_tamper_ts: float | None = None

        if self.settings.display_enabled and not self.is_single_camera:
            LOGGER.info(
                "Display preview disabled for camera '%s' (multi-camera mode active)",
                self.cam.camera_id,
            )

    def _default_model_factory(self) -> Any:
        """Instantiate a new YOLO model dedicated to this camera worker."""
        from ultralytics import YOLO

        return YOLO(self.settings.model_path)

    def _load_model(self) -> None:
        """Create or recreate the YOLO model instance."""
        LOGGER.info("Loading YOLO model for camera '%s'", self.cam.camera_id)
        self._model = self._model_factory()
        LOGGER.info("YOLO model ready for camera '%s'", self.cam.camera_id)

    def is_tamper_active(self) -> bool:
        """Return True if a CAMERA_TAMPER event was emitted within the last 30s."""
        if self._last_tamper_ts is None:
            return False
        return (self._clock() - self._last_tamper_ts) < 30.0

    @property
    def status(self) -> str:
        """Return camera lifecycle status, reporting DEGRADED when tampering or FPS < 3."""
        reader_status = self.reader.status.value
        if reader_status == "LIVE" and (self.is_tamper_active() or self.reader.fps < 3.0):
            return "DEGRADED"
        return reader_status

    def _apply_severity(self, event: dict[str, Any]) -> None:
        """Assign severity level to an event dictionary via assign_severity."""
        event_type_str = str(event.get("event_type", ""))
        try:
            ev_type = EventType(event_type_str)
        except (ValueError, KeyError):
            LOGGER.warning(
                "Invalid event_type '%s' for camera '%s'; defaulting severity to MEDIUM",
                event_type_str,
                self.cam.camera_id,
            )
            event["severity"] = Severity.MEDIUM.value
            return

        candidate = EventCandidate(
            event_type=ev_type,
            object_type=event.get("object_type"),
            track_id=event.get("track_id"),
            confidence=float(event.get("confidence", 1.0)),
            extra=event.get("extra") if isinstance(event.get("extra"), dict) else {},
        )
        try:
            sev = assign_severity(candidate, datetime.now())
            event["severity"] = sev.value
        except Exception:
            LOGGER.exception(
                "assign_severity failed for event_type '%s'; defaulting to MEDIUM",
                event_type_str,
            )
            event["severity"] = Severity.MEDIUM.value

    def _handle_tamper_candidate(self, cand: EventCandidate, capture_ts: float) -> None:
        """Transform a health candidate into a persisted CAMERA_TAMPER event."""
        latency_ms = max(0.0, (self._clock() - capture_ts) * 1000.0)
        latency_int = round(latency_ms)

        extra = dict(cand.extra) if isinstance(cand.extra, dict) else {}
        extra["latency_ms"] = latency_int

        event = {
            "event_id": str(uuid4()),
            "camera_id": self.cam.camera_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": cand.event_type.value if hasattr(cand.event_type, "value") else str(cand.event_type),
            "object_type": "camera",
            "track_id": 0,
            "confidence": float(cand.confidence),
            "evidence_path": "",
            "ack_status": "pending",
            "extra": extra,
        }
        self._apply_severity(event)
        self._last_tamper_ts = self._clock()
        self._dispatch_event(event)

    def _default_event_sink(
        self,
        event: dict[str, Any],
        frame: np.ndarray | None = None,
        detection: TrackedDetection | None = None,
    ) -> None:
        """Persist evidence crops and database event rows."""
        self._persist_event(event, frame, detection)

    def _persist_event(
        self,
        event: dict[str, Any],
        frame: np.ndarray | None = None,
        detection: TrackedDetection | None = None,
    ) -> None:
        """Persist evidence crops and database event rows."""
        if frame is not None and detection is not None and event.get("evidence_path"):
            try:
                save_evidence_snapshot(
                    frame,
                    detection,
                    Path(str(event["evidence_path"])),
                    self.cam.location,
                    str(event["timestamp"]),
                )
            except (OSError, cv2.error) as exc:
                LOGGER.warning(
                    "Failed to save evidence snapshot for %s: %s",
                    event.get("event_id"),
                    exc,
                )

        try:
            insert_event(
                event,
                self.settings.database_path,
                severity=event.get("severity", "MEDIUM"),
                extra=event.get("extra") if isinstance(event.get("extra"), dict) else None,
            )
        except (sqlite3.Error, OSError) as exc:
            LOGGER.error("Failed to store event %s: %s", event.get("event_id"), exc)
            return

        if self.enricher is not None and event.get("event_id"):
            self._submit_to_ai_enricher(event)

    def _submit_to_ai_enricher(self, event: dict[str, Any]) -> None:
        """Apply rule-based fallback and submit event to background AI enricher."""
        event_id = str(event["event_id"])
        rule_res = rule_based_analysis(event)
        event_type = event.get("event_type")
        is_gemini_eligible = event_type in WHITELISTED_EVENT_TYPES

        if not self.enricher.enabled:
            initial_status = "disabled"
        elif is_gemini_eligible:
            initial_status = "pending"
        else:
            initial_status = "done"

        try:
            update_event_ai(
                event_id,
                gemini_summary=rule_res.summary,
                false_alarm_rating=rule_res.false_alarm_rating,
                rating_reason=rule_res.rating_reason,
                ai_source="rules",
                ai_status=initial_status,
                db_path=self.settings.database_path,
            )
        except (sqlite3.Error, OSError) as exc:
            LOGGER.error("Failed to set initial AI status for event %s: %s", event_id, exc)

        evidence_path = event.get("evidence_path")

        def _lazy_load_image() -> bytes | None:
            if not evidence_path:
                return None
            p = Path(str(evidence_path))
            if p.is_file():
                return p.read_bytes()
            return None

        try:
            self.enricher.submit(
                event_id=event_id,
                meta=event,
                image_loader=_lazy_load_image,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Failed to submit event %s to enricher: %s", event_id, exc)

    def _dispatch_event(
        self,
        event: dict[str, Any],
        frame: np.ndarray | None = None,
        detection: TrackedDetection | None = None,
    ) -> None:
        """Dispatch event through the configured sink with fallback signatures."""
        if "severity" not in event or not event["severity"]:
            self._apply_severity(event)
        try:
            self._event_sink(event, frame=frame, detection=detection)
        except TypeError:
            try:
                self._event_sink(event, frame, detection)
            except TypeError:
                self._event_sink(event)

    def _on_status_change(self, old_status: StreamStatus, new_status: StreamStatus) -> None:
        """Handle reader status transitions and emit system events."""
        LOGGER.info(
            "Camera '%s' status changed: %s -> %s",
            self.cam.camera_id,
            old_status.value,
            new_status.value,
        )
        if new_status == StreamStatus.OFFLINE:
            self._emit_status_event("CAMERA_OFFLINE")
        elif new_status == StreamStatus.LIVE and old_status == StreamStatus.OFFLINE:
            self._emit_status_event("CAMERA_ONLINE")

    def _emit_status_event(self, event_type: str) -> None:
        """Emit a camera connection lifecycle event using schema-compatible sentinels."""
        event = {
            "event_id": str(uuid4()),
            "camera_id": self.cam.camera_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            "object_type": "camera",
            "track_id": 0,
            "confidence": 1.0,
            "evidence_path": "",
            "ack_status": "pending",
            "extra": {},
        }
        self._apply_severity(event)
        try:
            self._dispatch_event(event)
        except (sqlite3.Error, OSError):
            LOGGER.exception(
                "Failed to dispatch status event for camera '%s'",
                self.cam.camera_id,
            )

    def update_geometry(
        self,
        fence: FenceConfig,
        zone: list[list[float]],
    ) -> None:
        """Update virtual fence and restricted zone coordinates under lock."""
        with self._geom_lock:
            self.cam = self.cam.model_copy(update={"fence": fence, "zone": zone})
            if self._current_frame_size is not None:
                self._rebuild_geometry(self._current_frame_size[0], self._current_frame_size[1])

    def _ensure_geometry(self, width: int, height: int) -> None:
        """Ensure geometry is resolved for the given frame dimensions."""
        with self._geom_lock:
            if self._current_frame_size == (width, height):
                return
            self._current_frame_size = (width, height)
            self._rebuild_geometry(width, height)

    def _rebuild_geometry(self, width: int, height: int) -> None:
        """Reconstruct fence and zone event managers from resolved geometry."""
        resolved = resolve_geometry(self.cam, width, height)
        self._fence_event_manager = FenceEventManager(
            camera_id=self.cam.camera_id,
            fence=Fence(start=resolved.fence_start, end=resolved.fence_end),
            cooldown_seconds=self.cam.cooldown_seconds,
        )
        self._zone_event_manager = RestrictedZoneEventManager(
            camera_id=self.cam.camera_id,
            zone=RestrictedZone(points=tuple(resolved.zone)),
            cooldown_seconds=self.cam.cooldown_seconds,
        )

    def start(self) -> None:
        """Start the stream reader and worker thread (idempotent)."""
        with self._lock:
            if self._started and self._thread is not None and self._thread.is_alive():
                return
            self._started = True
            self._stop_event.clear()
            self.reader.start()
            self._thread = threading.Thread(
                target=self._run,
                name=f"CameraWorker-{self.cam.camera_id}",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop worker processing and underlying stream reader (idempotent)."""
        with self._lock:
            if not self._started and self._thread is None:
                return
            self._stop_event.set()
            thread = self._thread
            self._started = False

        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

        self.reader.stop(timeout=timeout)
        with self._lock:
            self._thread = None

        if self.settings.display_enabled and self.is_single_camera:
            try:
                cv2.destroyAllWindows()
            except cv2.error:
                pass

    def _wait(self, delay: float) -> bool:
        """Interruptible sleep returning True if stop requested."""
        if self._sleep is time.sleep:
            return self._stop_event.wait(delay)
        self._sleep(delay)
        return self._stop_event.wait(0.001)

    def _count_active_objects(self, detections: list[TrackedDetection]) -> dict[str, int]:
        """Tally unique tracked object IDs grouped by category."""
        track_ids: dict[str, set[int]] = {"person": set(), "vehicle": set(), "animal": set()}
        for detection in detections:
            obj_type = detection.object_type
            if obj_type in track_ids:
                track_ids[obj_type].add(detection.track_id)
        return {k: len(v) for k, v in track_ids.items()}

    def _run(self) -> None:
        """Continuously process latest frames from the reader."""
        self._load_model()
        last_processed_ts: float | None = None
        frame_number = 0
        last_result: Any = None
        last_counts: dict[str, int] = {"person": 0, "vehicle": 0, "animal": 0}
        consecutive_failures = 0

        while not self._stop_event.is_set():
            latest = self.reader.latest()
            if latest is None:
                if self._wait(0.005):
                    break
                continue

            raw_frame, capture_ts = latest
            if capture_ts == last_processed_ts:
                if self._wait(0.005):
                    break
                continue
            last_processed_ts = capture_ts

            try:
                # Frame health monitoring before detection (flag analytics.health)
                if self._health_monitor is not None:
                    now_mono = self._clock()
                    if now_mono >= self._health_disabled_until:
                        try:
                            health_candidates = self._health_monitor.update(raw_frame, now_mono)
                            for cand in health_candidates:
                                self._handle_tamper_candidate(cand, capture_ts)
                        except Exception:
                            LOGGER.exception(
                                "Health monitor failed for camera '%s'; disabling for 60s",
                                self.cam.camera_id,
                            )
                            self._health_disabled_until = now_mono + 60.0

                frame = raw_frame
                # Resize if target_width is configured
                if self.cam.target_width is not None and self.cam.target_width > 0:
                    orig_h, orig_w = frame.shape[:2]
                    if orig_w != self.cam.target_width:
                        scale = self.cam.target_width / orig_w
                        new_h = max(1, round(orig_h * scale))
                        frame = cv2.resize(frame, (self.cam.target_width, new_h))

                h, width = frame.shape[:2]
                self._ensure_geometry(width, h)

                frame_number += 1
                should_infer = (
                    self.cam.inference_stride <= 1
                    or ((frame_number - 1) % self.cam.inference_stride == 0)
                    or (last_result is None)
                )

                if should_infer:
                    results = self._model.track(
                        frame,
                        persist=True,
                        conf=self.settings.confidence_threshold,
                        classes=list(self.settings.class_filter),
                        tracker=self.settings.tracker_type,
                        verbose=False,
                    )
                    if isinstance(results, list):
                        last_result = results[0] if results else None
                    else:
                        last_result = results
                    detections = (
                        extract_tracked_detections(last_result)
                        if last_result is not None
                        else []
                    )

                    latency_ms = max(0.0, (self._clock() - capture_ts) * 1000.0)
                    latency_int = round(latency_ms)

                    # Handle first-seen tracks
                    for detection in detections:
                        if detection.track_id not in self.seen_track_ids:
                            self.seen_track_ids.add(detection.track_id)
                            LOGGER.info(
                                "Camera '%s' new track: %s #%s",
                                self.cam.camera_id,
                                detection.class_name,
                                detection.track_id,
                            )
                            event = build_new_object_event(self.cam.camera_id, detection)
                            existing_extra = event.get("extra")
                            extra_dict = dict(existing_extra) if isinstance(existing_extra, dict) else {}
                            extra_dict["latency_ms"] = latency_int
                            event["extra"] = extra_dict
                            self._apply_severity(event)
                            self._dispatch_event(event)

                    # Evaluate fence and zone crossings
                    with self._geom_lock:
                        fem = self._fence_event_manager
                        zem = self._zone_event_manager

                    events: list[dict[str, Any]] = []
                    if fem is not None:
                        events.extend(fem.process(detections, self.settings.evidence_dir))
                    if zem is not None and len(zem.zone.points) >= 3:
                        events.extend(zem.process(detections, self.settings.evidence_dir))

                    det_map = {d.track_id: d for d in detections}
                    for ev in events:
                        existing_extra = ev.get("extra")
                        extra_dict = dict(existing_extra) if isinstance(existing_extra, dict) else {}
                        extra_dict["latency_ms"] = latency_int
                        ev["extra"] = extra_dict
                        self._apply_severity(ev)
                        track_det = det_map.get(int(ev["track_id"]))
                        self._dispatch_event(ev, frame=frame, detection=track_det)

                    last_counts = self._count_active_objects(detections)

                # Annotation
                annotated = frame.copy()
                with self._geom_lock:
                    fem = self._fence_event_manager
                    zem = self._zone_event_manager

                if fem is not None:
                    draw_fence(annotated, fem.fence)
                if zem is not None and len(zem.zone.points) >= 3:
                    draw_restricted_zone(annotated, zem.zone)
                if last_result is not None:
                    draw_detections(annotated, last_result)
                draw_counts_overlay(annotated, last_counts)

                latency_ms = (self._clock() - capture_ts) * 1000.0
                LOGGER.debug("Camera '%s' frame latency: %.2f ms", self.cam.camera_id, latency_ms)

                if self.live_state is not None:
                    self.live_state.publish(
                        camera_id=self.cam.camera_id,
                        annotated_frame=annotated,
                        counts=last_counts,
                        status=self.status,
                        fps=self.reader.fps,
                        latency_ms=latency_ms,
                        last_frame_ts=capture_ts,
                        raw_frame=raw_frame,
                        tamper_active=self.is_tamper_active(),
                    )

                if self.settings.display_enabled and self.is_single_camera:
                    cv2.imshow(config.WINDOW_NAME, annotated)
                    key = cv2.waitKey(1) & 0xFF
                    if key == ord(config.EXIT_KEY):
                        LOGGER.info("Exit key pressed; stopping camera worker")
                        break

                consecutive_failures = 0

            except Exception:
                consecutive_failures += 1
                LOGGER.exception(
                    "Error in camera worker '%s' (failure %d/5)",
                    self.cam.camera_id,
                    consecutive_failures,
                )
                if consecutive_failures >= 5:
                    LOGGER.error(
                        "Camera '%s' reached 5 consecutive failures; backoff and recreating model",
                        self.cam.camera_id,
                    )
                    if self._wait(1.0):
                        break
                    try:
                        self._load_model()
                    except Exception:
                        LOGGER.exception(
                            "Failed to recreate model for camera '%s'",
                            self.cam.camera_id,
                        )
                    consecutive_failures = 0
