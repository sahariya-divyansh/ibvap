"""Smoke test for backend.analytics stubs."""

from __future__ import annotations

from datetime import datetime

import numpy as np

from backend.analytics.clips import ClipRecorder
from backend.analytics.direction import crossing_direction
from backend.analytics.health import FrameHealthMonitor
from backend.analytics.loitering import LoiteringDetector
from backend.analytics.night import enhance_low_light, is_low_light
from backend.analytics.sequence import SequenceDetector
from backend.analytics.severity import assign_severity, is_suppressed
from backend.analytics.trails import TrailBuffer, draw_trails
from backend.analytics.types import (
    EventCandidate,
    EventType,
    Severity,
    TrackedLike,
    TrackedObject,
)


def test_smoke_analytics_stubs() -> None:
    """Verify that every analytics module imports cleanly and stubs behave safely."""
    # 1. Types
    tracked_obj = TrackedObject(
        track_id=1,
        object_type="person",
        bbox=(10, 20, 30, 40),
        confidence=0.95,
    )
    assert isinstance(tracked_obj, TrackedLike)
    assert tracked_obj.center == (20.0, 30.0)

    candidate = EventCandidate(
        event_type=EventType.LOITERING,
        object_type="person",
        track_id=1,
        confidence=0.9,
    )
    assert candidate.event_type == EventType.LOITERING

    # 2. Severity (K1, K4)
    sev = assign_severity(candidate, datetime(2026, 10, 19, 12, 0))
    assert sev == Severity.MEDIUM
    assert is_suppressed(candidate) is False

    # 3. Loitering (K2)
    loiter_det = LoiteringDetector(zone=[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)])
    assert loiter_det.update([tracked_obj], 100.0) == []

    # 4. Direction (K3)
    direction = crossing_direction((0.0, 0.0), (1.0, 1.0), (0.0, 1.0), (1.0, 0.0))
    assert direction is None

    # 5. Sequence (K5)
    seq_det = SequenceDetector()
    assert seq_det.ingest(candidate, 100.0) == []

    # 6. Health (S1)
    frame = np.zeros((64, 64, 3), dtype=np.uint8)
    health_mon = FrameHealthMonitor()
    assert health_mon.update(frame, 100.0) == []

    # 7. Trails (S2)
    trails = TrailBuffer()
    trails.update([tracked_obj], 100.0)
    assert trails.points(1) == []
    draw_trails(frame, trails)

    # 8. Night (S4)
    assert is_low_light(frame) is False
    enhanced = enhance_low_light(frame)
    assert enhanced.shape == frame.shape
    assert enhanced is not frame

    # 9. Clips (S5)
    clip_rec = ClipRecorder()
    clip_rec.push(frame, 100.0)
    clip_rec.request("test_event", 100.0)
    assert clip_rec.poll(100.0, "evidence") == []
