"""Tests for backend.analytics.severity (K1)."""

from __future__ import annotations

import copy
import logging
from datetime import datetime
from types import MappingProxyType

import pytest

from backend.analytics import severity
from backend.analytics.severity import assign_severity
from backend.analytics.types import EventCandidate, EventType, Severity


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 19, hour, minute)


def test_person_fence_cross_afternoon_is_high() -> None:
    c = EventCandidate(event_type=EventType.VIRTUAL_FENCE_CROSS, object_type="person")
    assert assign_severity(c, _at(14)) == Severity.HIGH


def test_animal_fence_cross_is_low() -> None:
    c = EventCandidate(event_type=EventType.VIRTUAL_FENCE_CROSS, object_type="animal")
    assert assign_severity(c, _at(14)) == Severity.LOW


def test_animal_zone_entry_is_low() -> None:
    c = EventCandidate(event_type=EventType.RESTRICTED_ZONE_ENTRY, object_type="animal")
    assert assign_severity(c, _at(14)) == Severity.LOW


def test_person_loitering_afternoon_is_medium() -> None:
    c = EventCandidate(event_type=EventType.LOITERING, object_type="person")
    assert assign_severity(c, _at(14)) == Severity.MEDIUM


def test_person_loitering_2330_is_high() -> None:
    c = EventCandidate(event_type=EventType.LOITERING, object_type="person")
    assert assign_severity(c, _at(23, 30)) == Severity.HIGH


def test_person_loitering_0459_is_high() -> None:
    c = EventCandidate(event_type=EventType.LOITERING, object_type="person")
    assert assign_severity(c, _at(4, 59)) == Severity.HIGH


def test_person_loitering_0500_is_medium() -> None:
    c = EventCandidate(event_type=EventType.LOITERING, object_type="person")
    assert assign_severity(c, _at(5, 0)) == Severity.MEDIUM


def test_animal_loitering_2330_is_low() -> None:
    """Animal suppression takes precedence over the night modifier."""
    c = EventCandidate(event_type=EventType.LOITERING, object_type="animal")
    assert assign_severity(c, _at(23, 30)) == Severity.LOW


def test_camera_online_is_low() -> None:
    c = EventCandidate(event_type=EventType.CAMERA_ONLINE)
    assert assign_severity(c, _at(14)) == Severity.LOW


def test_camera_offline_is_high() -> None:
    c = EventCandidate(event_type=EventType.CAMERA_OFFLINE)
    assert assign_severity(c, _at(14)) == Severity.HIGH


def test_new_object_detected_is_low() -> None:
    c = EventCandidate(event_type=EventType.NEW_OBJECT_DETECTED, object_type="person")
    assert assign_severity(c, _at(14)) == Severity.LOW


def test_candidate_is_not_mutated() -> None:
    c = EventCandidate(
        event_type=EventType.LOITERING,
        object_type="person",
        track_id=7,
        confidence=0.8,
        extra={"dwell_seconds": 12.4},
    )
    before = copy.deepcopy(c)
    assign_severity(c, _at(23, 30))
    assert c == before
    assert c.severity is None
    assert c.extra == {"dwell_seconds": 12.4}


def test_unknown_event_type_falls_back_to_medium_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Simulate an event type unknown to the severity table by removing a real
    # entry from BASE_SEVERITY, rather than inventing a non-enum value.
    reduced = MappingProxyType(
        {k: v for k, v in severity.BASE_SEVERITY.items() if k != EventType.CAMERA_TAMPER}
    )
    monkeypatch.setattr(severity, "BASE_SEVERITY", reduced)

    c = EventCandidate(event_type=EventType.CAMERA_TAMPER)
    with caplog.at_level(logging.WARNING, logger=severity.LOGGER.name):
        result = assign_severity(c, _at(14))

    assert result == Severity.MEDIUM
    assert any(record.levelno == logging.WARNING for record in caplog.records)
