"""Tests for Gemini wiring into database, camera worker, API endpoints, and configuration."""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import backend.main
from backend.camera_config import CameraConfig, FenceConfig
from backend.camera_worker import CameraWorker
from backend.config import Settings
from backend.db import get_event, init_db, insert_event, update_event_ai
from backend.main import app


# ---------------------------------------------------------------------------
# 1. Migration idempotent
# ---------------------------------------------------------------------------
def test_migration_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "test_migration.db"
    # First init
    init_db(db_path)

    # Insert test row
    event = {
        "event_id": "mig-01",
        "camera_id": "cam_01",
        "timestamp": "2026-10-20T12:00:00Z",
        "event_type": "VIRTUAL_FENCE_CROSS",
        "object_type": "person",
        "track_id": 1,
        "confidence": 0.9,
        "evidence_path": "",
    }
    insert_event(event, db_path)

    # Run init multiple times idempotently
    init_db(db_path)
    init_db(db_path)

    row = get_event("mig-01", db_path)
    assert row is not None
    assert row["event_id"] == "mig-01"
    assert "gemini_summary" in row
    assert "false_alarm_rating" in row
    assert "rating_reason" in row
    assert "ai_source" in row
    assert "ai_status" in row
    assert "ai_updated_at" in row


# ---------------------------------------------------------------------------
# 2. update_event_ai round trip
# ---------------------------------------------------------------------------
def test_update_event_ai_round_trip(tmp_path: Path) -> None:
    db_path = tmp_path / "test_update_ai.db"
    init_db(db_path)

    event = {
        "event_id": "ev-ai-01",
        "camera_id": "cam_01",
        "timestamp": "2026-10-20T12:00:00Z",
        "event_type": "VIRTUAL_FENCE_CROSS",
        "object_type": "person",
        "track_id": 42,
        "confidence": 0.85,
        "evidence_path": "",
    }
    insert_event(event, db_path)

    updated = update_event_ai(
        "ev-ai-01",
        gemini_summary="Person crossed fence line under low light.",
        false_alarm_rating="LOW",
        rating_reason="Clear detection of human crossing.",
        ai_source="gemini",
        ai_status="done",
        ai_updated_at="2026-10-20T12:00:05Z",
        db_path=db_path,
    )

    assert updated is not None
    assert updated["gemini_summary"] == "Person crossed fence line under low light."
    assert updated["false_alarm_rating"] == "LOW"
    assert updated["rating_reason"] == "Clear detection of human crossing."
    assert updated["ai_source"] == "gemini"
    assert updated["ai_status"] == "done"
    assert updated["ai_updated_at"] == "2026-10-20T12:00:05Z"

    # Query afresh from DB
    fetched = get_event("ev-ai-01", db_path)
    assert fetched == updated


# ---------------------------------------------------------------------------
# 3. Worker submits only after persist and never blocks
# ---------------------------------------------------------------------------
def test_worker_submits_only_after_persist_and_never_blocks(tmp_path: Path) -> None:
    db_path = tmp_path / "test_worker.db"
    init_db(db_path)

    evidence_file = tmp_path / "test_ev.jpg"
    evidence_file.write_bytes(b"\xff\xd8\xff\xe0dummy_jpeg")

    submitted_events: list[dict[str, Any]] = []

    class FakeSlowEnricher:
        enabled = True

        def submit(self, event_id: str, meta: dict[str, Any], image_loader: Any = None, force: bool = False) -> bool:
            # Check DB: row must already exist when submit is called!
            row_in_db = get_event(event_id, db_path)
            assert row_in_db is not None, "Event was not persisted before submit!"
            submitted_events.append({"event_id": event_id, "meta": meta})
            # Simulate non-blocking test: even if this loader is evaluated later, submit itself is fast
            return True

    fake_enricher = FakeSlowEnricher()

    cam = CameraConfig(
        camera_id="cam_01",
        location="Gate 1",
        source="0",
        fence=FenceConfig(start=[0.0, 0.5], end=[1.0, 0.5]),
    )
    settings = Settings(
        model_path="yolov8n.pt",
        confidence_threshold=0.35,
        class_filter=(0,),
        video_source="0",
        frame_log_interval=30,
        tracker_type="bytetrack.yaml",
        camera_id="cam_01",
        location_label="Gate 1",
        fence_start=(0.0, 0.5),
        fence_end=(1.0, 0.5),
        restricted_zone=((0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)),
        event_cooldown_seconds=4.0,
        evidence_dir=tmp_path,
        database_path=db_path,
        display_enabled=False,
    )

    worker = CameraWorker(
        cam=cam,
        settings=settings,
        enricher=fake_enricher,
    )

    event = {
        "event_id": "ev-persist-test",
        "camera_id": "cam_01",
        "timestamp": "2026-10-20T12:00:00Z",
        "event_type": "VIRTUAL_FENCE_CROSS",
        "object_type": "person",
        "track_id": 10,
        "confidence": 0.8,
        "evidence_path": str(evidence_file),
    }

    start = time.perf_counter()
    worker._persist_event(event)
    duration = time.perf_counter() - start

    assert duration < 0.2  # Must not block
    assert len(submitted_events) == 1
    assert submitted_events[0]["event_id"] == "ev-persist-test"

    stored = get_event("ev-persist-test", db_path)
    assert stored is not None
    assert stored["ai_status"] == "pending"
    assert stored["ai_source"] == "rules"


# ---------------------------------------------------------------------------
# 4. Disabled mode
# ---------------------------------------------------------------------------
def test_worker_disabled_mode(tmp_path: Path) -> None:
    db_path = tmp_path / "test_disabled.db"
    init_db(db_path)

    class FakeDisabledEnricher:
        enabled = False

        def submit(self, event_id: str, meta: dict[str, Any], image_loader: Any = None, force: bool = False) -> bool:
            return False

    cam = CameraConfig(
        camera_id="cam_01",
        location="Gate 1",
        source="0",
        fence=FenceConfig(start=[0.0, 0.5], end=[1.0, 0.5]),
    )
    settings = Settings(
        model_path="yolov8n.pt",
        confidence_threshold=0.35,
        class_filter=(0,),
        video_source="0",
        frame_log_interval=30,
        tracker_type="bytetrack.yaml",
        camera_id="cam_01",
        location_label="Gate 1",
        fence_start=(0.0, 0.5),
        fence_end=(1.0, 0.5),
        restricted_zone=((0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)),
        event_cooldown_seconds=4.0,
        evidence_dir=tmp_path,
        database_path=db_path,
        display_enabled=False,
    )

    worker = CameraWorker(cam=cam, settings=settings, enricher=FakeDisabledEnricher())

    event = {
        "event_id": "ev-disabled-test",
        "camera_id": "cam_01",
        "timestamp": "2026-10-20T12:00:00Z",
        "event_type": "VIRTUAL_FENCE_CROSS",
        "object_type": "animal",
        "track_id": 11,
        "confidence": 0.7,
        "evidence_path": "",
    }

    worker._persist_event(event)

    stored = get_event("ev-disabled-test", db_path)
    assert stored is not None
    assert stored["ai_status"] == "disabled"
    assert stored["ai_source"] == "rules"
    assert stored["false_alarm_rating"] == "HIGH"


# ---------------------------------------------------------------------------
# 5. /ai/status and /events/{id}/analyze with fake enricher in app.state
# ---------------------------------------------------------------------------
def test_ai_status_and_analyze_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "test_api_enricher.db"
    init_db(db_path)
    old_settings = backend.main.settings
    new_settings = dataclasses.replace(old_settings, database_path=db_path)
    monkeypatch.setattr(backend.main, "settings", new_settings)

    # Insert test event
    event = {
        "event_id": "ev-api-test",
        "camera_id": "cam_01",
        "timestamp": "2026-10-20T12:00:00Z",
        "event_type": "VIRTUAL_FENCE_CROSS",
        "object_type": "person",
        "track_id": 99,
        "confidence": 0.88,
        "evidence_path": "",
    }
    insert_event(event, db_path)

    client = TestClient(app)

    # 1. Test GET /ai/status with fake enricher
    fake_enricher = MagicMock()
    fake_enricher.enabled = True
    fake_enricher.analyzer.model = "test-gemini-2.5"
    fake_enricher.get_status.return_value = {
        "enabled": True,
        "online": True,
        "queue_size": 2,
        "calls_last_minute": 5,
        "last_error": None,
    }
    app.state.ai_enricher = fake_enricher

    resp = client.get("/ai/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is True
    assert data["model"] == "test-gemini-2.5"
    assert data["online"] is True
    assert data["queue_size"] == 2
    assert data["calls_last_minute"] == 5

    # 2. Test POST /events/{id}/analyze
    # Unknown ID -> 404
    resp_404 = client.post("/events/unknown-id/analyze")
    assert resp_404.status_code == 404

    # Disabled enricher -> 503
    fake_enricher.enabled = False
    resp_503 = client.post("/events/ev-api-test/analyze")
    assert resp_503.status_code == 503

    # Enabled enricher -> 202
    fake_enricher.enabled = True
    resp_202 = client.post("/events/ev-api-test/analyze")
    assert resp_202.status_code == 202
    assert fake_enricher.submit.called
    assert fake_enricher.submit.call_args[1]["force"] is True


# ---------------------------------------------------------------------------
# 6. New fields appear in /events
# ---------------------------------------------------------------------------
def test_new_fields_appear_in_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "test_events_feed.db"
    init_db(db_path)
    old_settings = backend.main.settings
    new_settings = dataclasses.replace(old_settings, database_path=db_path)
    monkeypatch.setattr(backend.main, "settings", new_settings)

    insert_event(
        {
            "event_id": "ev-feed-1",
            "camera_id": "cam_01",
            "timestamp": "2026-10-20T12:00:00Z",
            "event_type": "VIRTUAL_FENCE_CROSS",
            "object_type": "person",
            "track_id": 1,
            "confidence": 0.9,
            "evidence_path": "",
        },
        db_path,
    )
    update_event_ai(
        "ev-feed-1",
        gemini_summary="Person detected near gate.",
        false_alarm_rating="LOW",
        rating_reason="Confirmed person presence.",
        ai_source="gemini",
        ai_status="done",
        db_path=db_path,
    )

    insert_event(
        {
            "event_id": "ev-feed-2",
            "camera_id": "cam_01",
            "timestamp": "2026-10-20T12:01:00Z",
            "event_type": "VIRTUAL_FENCE_CROSS",
            "object_type": "animal",
            "track_id": 2,
            "confidence": 0.9,
            "evidence_path": "",
        },
        db_path,
    )
    update_event_ai(
        "ev-feed-2",
        gemini_summary="Dog near fence.",
        false_alarm_rating="HIGH",
        rating_reason="Animal trigger.",
        ai_source="rules",
        ai_status="done",
        db_path=db_path,
    )

    client = TestClient(app)

    # Unfiltered /events
    resp = client.get("/events")
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) == 2
    ev1 = next(e for e in events if e["event_id"] == "ev-feed-1")
    assert ev1["gemini_summary"] == "Person detected near gate."
    assert ev1["false_alarm_rating"] == "LOW"
    assert ev1["rating_reason"] == "Confirmed person presence."
    assert ev1["ai_source"] == "gemini"
    assert ev1["ai_status"] == "done"
    assert ev1["ai_updated_at"] is not None

    # Filtered by false_alarm_rating=HIGH
    resp_filtered = client.get("/events?false_alarm_rating=HIGH")
    assert resp_filtered.status_code == 200
    filtered_events = resp_filtered.json()
    assert len(filtered_events) == 1
    assert filtered_events[0]["event_id"] == "ev-feed-2"

    # Invalid false_alarm_rating -> 422
    resp_bad = client.get("/events?false_alarm_rating=INVALID")
    assert resp_bad.status_code == 422


# ---------------------------------------------------------------------------
# 7. scripts/gemini_check.py tests
# ---------------------------------------------------------------------------
def test_gemini_check_no_key(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    from scripts.gemini_check import check_gemini

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    ret = check_gemini(load_env=False)
    assert ret == 1
    captured = capsys.readouterr()
    assert "FAIL:" in captured.out
    assert "GEMINI_API_KEY not configured" in captured.out


def test_gemini_check_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    from google import genai

    from scripts.gemini_check import check_gemini

    monkeypatch.setenv("GEMINI_API_KEY", "fake_key_123")
    monkeypatch.setenv("IBVAP_GEMINI_MODEL", "test-model-xyz")

    fake_client_instance = MagicMock()
    fake_model = MagicMock()
    fake_model.name = "models/test-model-xyz"
    fake_model.supported_actions = ["generateContent"]
    fake_client_instance.models.list.return_value = [fake_model]

    fake_resp = MagicMock()
    fake_resp.text = "OK"
    fake_client_instance.models.generate_content.return_value = fake_resp

    monkeypatch.setattr(genai, "Client", lambda **_: fake_client_instance)

    ret = check_gemini(load_env=False)
    assert ret == 0
    captured = capsys.readouterr()
    assert "OK: test-model-xyz" in captured.out

