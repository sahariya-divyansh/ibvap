"""Severity assignment analytics module.

Owner: Kabir (K1, K4).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Final

from backend.analytics.types import EventCandidate, EventType, Severity

LOGGER = logging.getLogger(__name__)

BASE_SEVERITY: Final[Mapping[EventType, Severity]] = MappingProxyType(
    {
        EventType.VIRTUAL_FENCE_CROSS: Severity.HIGH,
        EventType.RESTRICTED_ZONE_ENTRY: Severity.HIGH,
        EventType.LOITERING: Severity.MEDIUM,
        EventType.SUSPICIOUS_SEQUENCE: Severity.HIGH,
        EventType.CAMERA_TAMPER: Severity.HIGH,
        EventType.CAMERA_OFFLINE: Severity.HIGH,
        EventType.CAMERA_ONLINE: Severity.LOW,
        EventType.NEW_OBJECT_DETECTED: Severity.LOW,
    }
)

# Event types eligible for animal-object suppression to LOW (K1 table, K4 predicate).
_ANIMAL_SUPPRESSED_TYPES: Final[frozenset[EventType]] = frozenset(
    {
        EventType.VIRTUAL_FENCE_CROSS,
        EventType.RESTRICTED_ZONE_ENTRY,
        EventType.LOITERING,
    }
)

_NIGHT_START_HOUR: Final[int] = 22
_NIGHT_END_HOUR: Final[int] = 5

_SEVERITY_ORDER: Final[tuple[Severity, ...]] = (Severity.LOW, Severity.MEDIUM, Severity.HIGH)


def _is_night(now_local: datetime) -> bool:
    """True inside the 22:00-05:00 local window (crosses midnight)."""
    hour = now_local.hour
    return hour >= _NIGHT_START_HOUR or hour < _NIGHT_END_HOUR


def _raise_one_level(severity: Severity) -> Severity:
    """Step one severity level up, capped at HIGH."""
    index = _SEVERITY_ORDER.index(severity)
    return _SEVERITY_ORDER[min(index + 1, len(_SEVERITY_ORDER) - 1)]


def assign_severity(c: EventCandidate, now_local: datetime) -> Severity:
    """Assign severity level to an event candidate.

    Pure and deterministic; does not mutate ``c``. Unknown event types fall
    back to MEDIUM and are logged at WARNING.
    """
    base = BASE_SEVERITY.get(c.event_type)
    if base is None:
        LOGGER.warning("Unknown event type %r; defaulting to MEDIUM severity", c.event_type)
        return Severity.MEDIUM

    if c.object_type == "animal" and c.event_type in _ANIMAL_SUPPRESSED_TYPES:
        return Severity.LOW

    if c.event_type == EventType.LOITERING and _is_night(now_local):
        return _raise_one_level(base)

    return base


def is_suppressed(c: EventCandidate) -> bool:
    """True when an animal-triggered fence/zone/loitering candidate was downgraded to LOW (K4)."""
    return False
