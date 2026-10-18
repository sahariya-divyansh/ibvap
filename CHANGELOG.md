# Changelog

All notable changes to IBVAP are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/) (pre-1.0 while hackathon prototype).

Divyansh updates this file when merging each PR. Contributors propose the entry text in their PR description.

## [Unreleased]

### Added (planned for v1.0-demo)
- One-click release launcher run.bat, stop.bat, and camera pre-check script [D10]
- Dismiss reviewed alerts with smooth collapse animation, reviewed toggle, and pending alert counter [H3]
- Per-camera workers, camera manager, and thread-safe per-camera live state with legacy fallback [D4, D5]
- Threaded stream reader with latest-frame buffer, exponential backoff reconnect, and status machine [D3]
- Camera registry loader, schema validation, and geometry resolver with legacy fallback [D2]
- Multi-camera support via `cameras.json` (per-camera worker, YOLO/tracker, fence, zone) — D2–D6
- Auto-reconnecting stream reader; `CAMERA_OFFLINE` / `CAMERA_ONLINE` events — D3
- Severity levels (LOW/MEDIUM/HIGH) — K1
- Loitering / dwell-time detection — K2
- Direction-aware fence crossing — K3
- Animal suppression and "false alerts avoided" counter — K4
- Camera health monitor (blur, blackout, frozen) — S1
- Trajectory trails — S2
- KPI report script — S3
- Camera grid, calibration page, alert feed v2, filters — H1–H4
- `run.bat` one-click start — D10

### Changed
- API returns `evidence_url` instead of the absolute `evidence_path`
- Events gain `severity`, `extra`, `clip_url`

### Security
- Evidence file system paths are no longer exposed through the API

## [0.1.0] - Baseline (before team split)

### Added
- YOLOv8n detection for person, vehicle, and animal classes
- ByteTrack persistent tracking IDs
- Virtual fence crossing and restricted-zone entry events
- `NEW_OBJECT_DETECTED` informational events
- Live annotated MJPEG stream and current-frame counts
- Evidence crops stamped with timestamp and location
- Audible alarm and flashing banner for fence/zone events
- Confirm / Reject operator workflow
- FastAPI REST API with SQLite storage
- Vanilla JS dashboard with no build step
- `.env`-based configuration
