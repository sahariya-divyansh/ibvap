"""SQLite storage for IBVAP events."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.config import get_settings

_DB_LOCK = threading.Lock()


def _connect(path: Path) -> sqlite3.Connection:
    """Create a new SQLite connection configured with WAL and busy timeout."""
    connection = sqlite3.connect(path, timeout=5.0)
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def init_db(db_path: Path | None = None) -> None:
    """Create the SQLite database and events table if needed, applying migrations."""
    path = _resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _DB_LOCK, _connect(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                camera_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                object_type TEXT NOT NULL,
                track_id INTEGER NOT NULL,
                confidence REAL NOT NULL,
                evidence_path TEXT NOT NULL,
                ack_status TEXT NOT NULL DEFAULT 'pending',
                severity TEXT NOT NULL DEFAULT 'MEDIUM',
                extra TEXT NOT NULL DEFAULT '{}',
                clip_path TEXT,
                gemini_summary TEXT,
                false_alarm_rating TEXT,
                rating_reason TEXT,
                ai_source TEXT,
                ai_status TEXT NOT NULL DEFAULT 'disabled',
                ai_updated_at TEXT
            )
            """
        )
        _migrate_schema(connection)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_camera_id ON events(camera_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_event_type ON events(event_type)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_cam_ts ON events(camera_id, timestamp)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_false_alarm ON events(false_alarm_rating)"
        )


def _migrate_schema(connection: sqlite3.Connection) -> None:
    """Idempotently add missing columns to existing databases."""
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(events)").fetchall()
    }
    if "ack_status" not in columns:
        connection.execute(
            "ALTER TABLE events ADD COLUMN ack_status TEXT NOT NULL DEFAULT 'pending'"
        )
    if "severity" not in columns:
        connection.execute(
            "ALTER TABLE events ADD COLUMN severity TEXT NOT NULL DEFAULT 'MEDIUM'"
        )
    if "extra" not in columns:
        connection.execute(
            "ALTER TABLE events ADD COLUMN extra TEXT NOT NULL DEFAULT '{}'"
        )
    if "clip_path" not in columns:
        connection.execute(
            "ALTER TABLE events ADD COLUMN clip_path TEXT"
        )
    if "gemini_summary" not in columns:
        connection.execute(
            "ALTER TABLE events ADD COLUMN gemini_summary TEXT"
        )
    if "false_alarm_rating" not in columns:
        connection.execute(
            "ALTER TABLE events ADD COLUMN false_alarm_rating TEXT"
        )
    if "rating_reason" not in columns:
        connection.execute(
            "ALTER TABLE events ADD COLUMN rating_reason TEXT"
        )
    if "ai_source" not in columns:
        connection.execute(
            "ALTER TABLE events ADD COLUMN ai_source TEXT"
        )
    if "ai_status" not in columns:
        connection.execute(
            "ALTER TABLE events ADD COLUMN ai_status TEXT NOT NULL DEFAULT 'disabled'"
        )
    if "ai_updated_at" not in columns:
        connection.execute(
            "ALTER TABLE events ADD COLUMN ai_updated_at TEXT"
        )


def insert_event(
    event: dict[str, Any],
    db_path: Path | None = None,
    *,
    severity: str = "MEDIUM",
    extra: dict[str, Any] | None = None,
    clip_path: str | None = None,
) -> None:
    """Insert a structured event into SQLite."""
    path = _resolve_db_path(db_path)
    sev = event.get("severity") or severity
    ext = (
        event.get("extra")
        if event.get("extra") is not None
        else (extra if extra is not None else {})
    )
    if isinstance(ext, dict):
        ext_text = json.dumps(ext)
    elif isinstance(ext, str):
        ext_text = ext
    else:
        ext_text = "{}"
    cp = event.get("clip_path") or clip_path

    with _DB_LOCK, _connect(path) as connection:
        connection.execute(
            """
            INSERT INTO events (
                event_id,
                camera_id,
                timestamp,
                event_type,
                object_type,
                track_id,
                confidence,
                evidence_path,
                ack_status,
                severity,
                extra,
                clip_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(event["event_id"]),
                str(event["camera_id"]),
                str(event["timestamp"]),
                str(event["event_type"]),
                str(event["object_type"]),
                int(event["track_id"]),
                float(event["confidence"]),
                str(event.get("evidence_path", "")),
                str(event.get("ack_status", "pending")),
                str(sev),
                ext_text,
                str(cp) if cp is not None else None,
            ),
        )


def convert_event_row(row: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
    """Convert a database row into an API-compliant event dictionary."""
    data = dict(row)

    # Parse extra JSON text -> dict
    raw_extra = data.get("extra")
    if isinstance(raw_extra, dict):
        extra_dict = dict(raw_extra)
    elif isinstance(raw_extra, str):
        try:
            parsed = json.loads(raw_extra)
            extra_dict = parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            extra_dict = {}
    else:
        extra_dict = {}
    data["extra"] = extra_dict

    # Derive evidence_url from evidence_path
    ev_path = data.get("evidence_path")
    if ev_path:
        filename = Path(str(ev_path)).name
        data["evidence_url"] = f"/evidence/{filename}"
    else:
        data["evidence_url"] = None

    # Derive clip_url from clip_path
    cp_path = data.get("clip_path")
    if cp_path:
        filename = Path(str(cp_path)).name
        data["clip_url"] = f"/evidence/{filename}"
    else:
        data["clip_url"] = None

    if not data.get("severity"):
        data["severity"] = "MEDIUM"

    data["gemini_summary"] = data.get("gemini_summary")
    data["false_alarm_rating"] = data.get("false_alarm_rating")
    data["rating_reason"] = data.get("rating_reason")
    data["ai_source"] = data.get("ai_source")
    data["ai_status"] = data.get("ai_status") or "disabled"
    data["ai_updated_at"] = data.get("ai_updated_at")

    return data


def list_events(
    camera_id: str | None = None,
    event_type: str | None = None,
    severity: str | None = None,
    ack_status: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    false_alarm_rating: str | None = None,
    limit: int | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return events newest first with optional filters."""
    query = "SELECT * FROM events"
    clauses: list[str] = []
    params: list[Any] = []

    if camera_id is not None:
        clauses.append("camera_id = ?")
        params.append(camera_id)
    if event_type is not None:
        clauses.append("event_type = ?")
        params.append(event_type)
    if severity is not None:
        clauses.append("severity = ?")
        params.append(severity)
    if ack_status is not None:
        clauses.append("ack_status = ?")
        params.append(ack_status)
    if start_time is not None:
        clauses.append("timestamp >= ?")
        params.append(start_time)
    if end_time is not None:
        clauses.append("timestamp <= ?")
        params.append(end_time)
    if false_alarm_rating is not None:
        clauses.append("false_alarm_rating = ?")
        params.append(false_alarm_rating)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)

    query += " ORDER BY timestamp DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    rows = _fetch_all(query, params, db_path)
    return [convert_event_row(row) for row in rows]


def get_event(event_id: str, db_path: Path | None = None) -> dict[str, Any] | None:
    """Return one event by ID, or None when not found."""
    rows = _fetch_all(
        "SELECT * FROM events WHERE event_id = ?",
        [event_id],
        db_path,
    )
    if not rows:
        return None
    return convert_event_row(rows[0])


def update_event_ack(
    event_id: str,
    ack_status: str,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    """Update an event acknowledgement status and return the updated event."""
    path = _resolve_db_path(db_path)
    with _DB_LOCK, _connect(path) as connection:
        connection.row_factory = sqlite3.Row
        cursor = connection.execute(
            "UPDATE events SET ack_status = ? WHERE event_id = ?",
            (ack_status, event_id),
        )
        if cursor.rowcount == 0:
            return None
        row = connection.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    if row is None:
        return None
    return convert_event_row(dict(row))


def update_event_ai(
    event_id: str,
    *,
    gemini_summary: str | None = None,
    false_alarm_rating: str | None = None,
    rating_reason: str | None = None,
    ai_source: str | None = None,
    ai_status: str | None = None,
    ai_updated_at: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    """Update AI enrichment fields for an event and return updated row."""
    path = _resolve_db_path(db_path)
    updates: list[str] = []
    params: list[Any] = []

    if gemini_summary is not None:
        updates.append("gemini_summary = ?")
        params.append(gemini_summary)
    if false_alarm_rating is not None:
        updates.append("false_alarm_rating = ?")
        params.append(false_alarm_rating)
    if rating_reason is not None:
        updates.append("rating_reason = ?")
        params.append(rating_reason)
    if ai_source is not None:
        updates.append("ai_source = ?")
        params.append(ai_source)
    if ai_status is not None:
        updates.append("ai_status = ?")
        params.append(ai_status)

    now_iso = ai_updated_at or datetime.now(UTC).isoformat()
    updates.append("ai_updated_at = ?")
    params.append(now_iso)

    params.append(event_id)
    query = f"UPDATE events SET {', '.join(updates)} WHERE event_id = ?"

    with _DB_LOCK, _connect(path) as connection:
        connection.row_factory = sqlite3.Row
        cursor = connection.execute(query, params)
        if cursor.rowcount == 0:
            return None
        row = connection.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    if row is None:
        return None
    return convert_event_row(dict(row))


def _fetch_all(
    query: str,
    params: list[Any],
    db_path: Path | None,
) -> list[dict[str, Any]]:
    """Run a SELECT query and return rows as dictionaries."""
    path = _resolve_db_path(db_path)
    with _DB_LOCK, _connect(path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def _resolve_db_path(db_path: Path | None) -> Path:
    """Resolve a database path from argument or settings."""
    if db_path is not None:
        return db_path
    return get_settings().database_path
