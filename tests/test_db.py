"""Tests for SQLite database schema migration, concurrency, and row conversion [D7]."""

from __future__ import annotations

import concurrent.futures
import sqlite3
from pathlib import Path
from uuid import uuid4

from backend.db import convert_event_row, get_event, init_db, insert_event, list_events


def test_migration_from_old_schema_keeps_rows_and_provides_defaults(tmp_path: Path) -> None:
    """An existing Phase 4 SQLite DB without D7 columns is migrated with defaults."""
    db_file = tmp_path / "legacy.db"

    # Create old table schema without severity, extra, clip_path
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            """
            CREATE TABLE events (
                event_id TEXT PRIMARY KEY,
                camera_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                object_type TEXT NOT NULL,
                track_id INTEGER NOT NULL,
                confidence REAL NOT NULL,
                evidence_path TEXT NOT NULL,
                ack_status TEXT NOT NULL DEFAULT 'pending'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO events (
                event_id, camera_id, timestamp, event_type, object_type,
                track_id, confidence, evidence_path, ack_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "evt_old_1",
                "cam_01",
                "2026-10-18T12:00:00Z",
                "VIRTUAL_FENCE_CROSS",
                "person",
                42,
                0.92,
                "evidence/snap_old.jpg",
                "pending",
            ),
        )

    # Run init_db to execute migration
    init_db(db_file)

    # Verify column existence and defaults
    row = get_event("evt_old_1", db_file)
    assert row is not None
    assert row["event_id"] == "evt_old_1"
    assert row["severity"] == "MEDIUM"
    assert row["extra"] == {}
    assert row["clip_path"] is None
    assert row["evidence_url"] == "/evidence/snap_old.jpg"
    assert row["clip_url"] is None


def test_migration_idempotency(tmp_path: Path) -> None:
    """Calling init_db multiple times does not fail or alter existing data."""
    db_file = tmp_path / "idempotent.db"
    init_db(db_file)
    init_db(db_file)
    init_db(db_file)

    insert_event(
        {
            "event_id": "evt_test",
            "camera_id": "cam_01",
            "timestamp": "2026-10-18T12:01:00Z",
            "event_type": "NEW_OBJECT_DETECTED",
            "object_type": "person",
            "track_id": 1,
            "confidence": 0.88,
            "evidence_path": "",
        },
        db_path=db_file,
    )

    # Calling init_db again after inserts
    init_db(db_file)
    row = get_event("evt_test", db_file)
    assert row is not None
    assert row["event_id"] == "evt_test"


def test_insert_with_extra_and_clip_path_roundtrips(tmp_path: Path) -> None:
    """Events inserted with extra dictionary and clip_path persist and convert properly."""
    db_file = tmp_path / "extra.db"
    init_db(db_file)

    insert_event(
        {
            "event_id": "evt_extra",
            "camera_id": "cam_02",
            "timestamp": "2026-10-18T12:05:00Z",
            "event_type": "RESTRICTED_ZONE_ENTRY",
            "object_type": "vehicle",
            "track_id": 99,
            "confidence": 0.95,
            "evidence_path": "evidence/snap_99.jpg",
            "severity": "HIGH",
            "extra": {"latency_ms": 142, "direction": "A_TO_B"},
            "clip_path": "evidence/clip_99.gif",
        },
        db_path=db_file,
    )

    event = get_event("evt_extra", db_file)
    assert event is not None
    assert event["severity"] == "HIGH"
    assert event["extra"] == {"latency_ms": 142, "direction": "A_TO_B"}
    assert event["evidence_url"] == "/evidence/snap_99.jpg"
    assert event["clip_url"] == "/evidence/clip_99.gif"
    assert event["evidence_path"] == "evidence/snap_99.jpg"


def test_row_conversion_edge_cases() -> None:
    """convert_event_row handles null/empty paths, invalid extra JSON, and missing fields."""
    # Empty evidence_path -> evidence_url is None
    row1 = {
        "event_id": "1",
        "evidence_path": "",
        "clip_path": None,
        "extra": "{}",
        "severity": "",
    }
    res1 = convert_event_row(row1)
    assert res1["evidence_url"] is None
    assert res1["clip_url"] is None
    assert res1["extra"] == {}
    assert res1["severity"] == "MEDIUM"

    # Bad JSON in extra -> returns {}
    row2 = {
        "event_id": "2",
        "evidence_path": "foo/bar.jpg",
        "clip_path": "foo/clip.gif",
        "extra": "NOT_VALID_JSON{",
    }
    res2 = convert_event_row(row2)
    assert res2["evidence_url"] == "/evidence/bar.jpg"
    assert res2["clip_url"] == "/evidence/clip.gif"
    assert res2["extra"] == {}

    # Extra is already a dict
    row3 = {
        "event_id": "3",
        "extra": {"latency_ms": 50},
    }
    res3 = convert_event_row(row3)
    assert res3["extra"] == {"latency_ms": 50}


def test_concurrent_inserts_no_database_locked(tmp_path: Path) -> None:
    """4 threads each inserting 50 events concurrently produce no lock errors and all 200 rows exist."""
    db_file = tmp_path / "concurrent.db"
    init_db(db_file)

    def worker_task(thread_idx: int) -> list[str]:
        inserted_ids = []
        for i in range(50):
            event_id = f"t{thread_idx}_e{i}_{uuid4().hex[:6]}"
            insert_event(
                {
                    "event_id": event_id,
                    "camera_id": f"cam_{thread_idx}",
                    "timestamp": f"2026-10-18T12:00:{i:02d}Z",
                    "event_type": "NEW_OBJECT_DETECTED",
                    "object_type": "person",
                    "track_id": i,
                    "confidence": 0.9,
                    "evidence_path": "",
                    "severity": "LOW",
                    "extra": {"thread": thread_idx, "iter": i},
                },
                db_path=db_file,
            )
            inserted_ids.append(event_id)
        return inserted_ids

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(worker_task, idx) for idx in range(4)]
        results = [f.result() for f in futures]

    all_inserted_ids = [eid for sublist in results for eid in sublist]
    assert len(all_inserted_ids) == 200

    all_rows = list_events(limit=1000, db_path=db_file)
    assert len(all_rows) == 200
