"""Rule-based fallback summary and heuristics for event analysis."""

from __future__ import annotations

from typing import Any

from backend.gemini_client import AnalysisResult


def rule_based_analysis(meta: dict[str, Any]) -> AnalysisResult:
    """Generate deterministic rule-based analysis and false alarm rating from event metadata."""
    event_type = str(meta.get("event_type") or "UNKNOWN_EVENT")
    object_type = str(meta.get("object_type") or "")
    camera_id = str(meta.get("camera_id") or "unknown_camera")
    location = str(meta.get("location") or meta.get("location_label") or camera_id)
    raw_time = str(meta.get("timestamp") or meta.get("time") or "")
    time_str = raw_time[:19].replace("T", " ") if raw_time else "recent"
    severity = str(meta.get("severity") or "MEDIUM").upper()

    try:
        confidence = float(meta.get("confidence", 1.0))
    except (ValueError, TypeError):
        confidence = 1.0

    # Readable one-sentence summary
    if object_type:
        summary = (
            f"{severity} severity {event_type} involving {object_type} "
            f"at {location} ({camera_id}) recorded at {time_str}."
        )
    else:
        summary = (
            f"{severity} severity {event_type} "
            f"at {location} ({camera_id}) recorded at {time_str}."
        )

    if len(summary) > 240:
        summary = summary[:237] + "..."

    # Rating heuristics:
    # 1. CAMERA_TAMPER/OFFLINE -> LOW (a real camera problem)
    # 2. animal -> HIGH false-alarm likelihood
    # 3. person or vehicle fence/zone event with confidence >= 0.6 -> LOW
    # 4. confidence < 0.45 -> MEDIUM
    # 5. otherwise -> MEDIUM
    fence_or_zone = event_type in {"VIRTUAL_FENCE_CROSS", "RESTRICTED_ZONE_ENTRY"}
    obj_lower = object_type.lower()

    if event_type in {"CAMERA_TAMPER", "CAMERA_OFFLINE"}:
        false_alarm_rating = "LOW"
        rating_reason = f"Verified hardware or stream condition ({event_type}) requiring technical inspection."
    elif obj_lower == "animal":
        false_alarm_rating = "HIGH"
        rating_reason = "Wildlife or domestic animal activity represents a non-threat false perimeter alarm."
    elif obj_lower in {"person", "vehicle"} and fence_or_zone and confidence >= 0.6:
        false_alarm_rating = "LOW"
        rating_reason = f"High-confidence ({confidence:.2f}) {obj_lower} perimeter breach detected."
    elif confidence < 0.45:
        false_alarm_rating = "MEDIUM"
        rating_reason = f"Low detection confidence ({confidence:.2f}) increases probability of false alarm."
    else:
        false_alarm_rating = "MEDIUM"
        rating_reason = "Standard detection criteria met; visual confirmation by operator recommended."

    if len(rating_reason) > 160:
        rating_reason = rating_reason[:157] + "..."

    raw_tags = [event_type.lower()]
    if obj_lower:
        raw_tags.append(obj_lower)
    raw_tags.append(severity.lower())
    scene_tags = [tag[:30] for tag in raw_tags if tag][:5]

    return AnalysisResult(
        summary=summary,
        false_alarm_rating=false_alarm_rating,
        rating_reason=rating_reason,
        scene_tags=scene_tags,
        source="rules",
    )
