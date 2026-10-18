# IBVAP - Intelligent Border Video Analytics Platform

IBVAP is a real-time video analytics dashboard for border security teams monitoring virtual fences and restricted zones. It detects people, vehicles, and animals, tracks them across frames, raises alerts for boundary violations, and stores reviewable evidence.

## Problem Statement Context

This project targets Smart India Hackathon problem statement SIH 26187: improving border-area monitoring through intelligent video analytics. The practical problem is continuous surveillance of sensitive fence lines and restricted zones where operators need fast alerts, visual context, and evidence records without manually watching every camera feed at all times.

## Key Features

- Object detection for COCO person, vehicle, and animal classes.
- Persistent object tracking with track IDs through Ultralytics ByteTrack.
- Virtual fence line crossing detection using tracked object center points.
- Restricted zone entry detection using a configurable polygon.
- Live MJPEG video stream with annotated boxes, labels, track IDs, fence line, restricted zone, and count overlay.
- Real-time current-frame counts for people, vehicles, and animals.
- Audible dashboard alarm and flashing red banner for high-severity fence and zone events.
- Evidence crop capture for fence and restricted-zone events.
- Evidence image stamping with event timestamp and configured location label.
- Quiet `NEW_OBJECT_DETECTED` events when a new track ID first appears.
- Human-in-the-loop alert workflow with Confirm and Reject actions.
- REST API backed by SQLite for zero-setup event storage.
- Vanilla browser dashboard with no frontend build step.

## Architecture Overview

The backend starts a FastAPI server and launches the blocking detection loop in a background thread. The detector owns the camera or video file connection, so the MJPEG endpoint does not open a second camera stream. Instead, the detector publishes the latest annotated frame and current counts into a lock-protected shared buffer that API routes read from.

```text
Camera / Video File
        |
        v
OpenCV frame capture
        |
        v
YOLOv8n object detection
        |
        v
ByteTrack persistent tracking
        |
        v
Fence + restricted-zone event logic
        |
        +--> Evidence crop + timestamp/location stamp
        |
        +--> SQLite event storage
        |
        v
FastAPI REST API + MJPEG stream
        |
        v
Browser dashboard
  - MJPEG video panel
  - /stats polling
  - /events polling
  - Confirm / Reject workflow
```

## Tech Stack

- **Python 3.13**: development runtime used for this repository.
- **Ultralytics YOLOv8n**: lightweight detector suitable for CPU-only prototype inference.
- **ByteTrack via Ultralytics**: persistent track IDs without implementing a tracker from scratch.
- **OpenCV**: video capture, frame annotation, JPEG encoding, and evidence image stamping.
- **NumPy**: image array handling and polygon drawing support.
- **FastAPI**: REST API, lifecycle startup, CORS, static evidence serving, and MJPEG streaming.
- **Uvicorn**: ASGI server for running the FastAPI application.
- **SQLite**: zero-setup local event storage with simple indexing.
- **Vanilla HTML/CSS/JavaScript**: dashboard with no bundler, package manager, or frontend build step.

## Setup Instructions

These instructions are Windows-focused because the current development environment is Windows PowerShell.

### 1. Clone and Enter the Project

```powershell
git clone <your-repo-url>
cd ibvap-sih2026
```

### 2. Create a Python 3.13 Virtual Environment

Use Python 3.13 for this project. Avoid Python 3.14 for now because the computer-vision stack can lag behind newly released Python versions; if prebuilt wheels are unavailable, packages such as NumPy or OpenCV may attempt slow local builds or fail during installation.

```powershell
py -3.13 -m venv venv
.\venv\Scripts\Activate.ps1
```

If `py -3.13` is unavailable, install Python 3.13 and recreate the virtual environment.

### 3. Install Dependencies

```powershell
python -m pip install -r requirements.txt
```

### 4. Create Local Configuration

```powershell
Copy-Item .env.example .env
```

Edit `.env` as needed:

- `IBVAP_MODEL_PATH`: YOLO model path, default `yolov8n.pt`.
- `IBVAP_CONFIDENCE_THRESHOLD`: minimum detection confidence.
- `IBVAP_CLASS_FILTER`: comma-separated COCO class IDs to detect.
- `IBVAP_VIDEO_SOURCE`: camera index such as `0`, or a video file path.
- `IBVAP_FRAME_LOG_INTERVAL`: frame interval for FPS logging.
- `IBVAP_TRACKER_TYPE`: Ultralytics tracker config, default `bytetrack.yaml`.
- `IBVAP_CAMERA_ID`: camera identifier stored on events.
- `IBVAP_LOCATION_LABEL`: human-readable location burned into evidence photos.
- `IBVAP_FENCE_START` and `IBVAP_FENCE_END`: virtual fence endpoints.
- `IBVAP_RESTRICTED_ZONE`: semicolon-separated polygon points.
- `IBVAP_EVENT_COOLDOWN_SECONDS`: per-track cooldown for fence and zone events.
- `IBVAP_EVIDENCE_DIR`: directory for saved evidence crops.
- `IBVAP_DATABASE_PATH`: SQLite database path.
- `IBVAP_DISPLAY_ENABLED`: whether to also open an OpenCV preview window.

Fence and zone coordinates from `0.0` to `1.0` are treated as normalized frame coordinates. For example, `0.0,0.5` to `1.0,0.5` creates a horizontal fence line halfway down the frame.

### 5. Start the Backend

```powershell
uvicorn backend.main:app --reload
```

The FastAPI lifespan hook initializes SQLite, creates the evidence directory, and starts the detector in a background thread.

### 6. Start the Frontend Server

Run this in a second PowerShell window from the project root:

```powershell
python -m http.server 8080 -d frontend
```

### 7. Open the URLs

```text
Dashboard:       http://localhost:8080/
API health:      http://localhost:8000/health
Event feed:      http://localhost:8000/events
Live stats:      http://localhost:8000/stats
MJPEG stream:    http://localhost:8000/video_feed
Evidence images: http://localhost:8000/evidence/<filename>.jpg
```

## How To Use / Demo Walkthrough

1. Start the backend with Uvicorn.
2. Start the frontend with `python -m http.server 8080 -d frontend`.
3. Open `http://localhost:8080/`.
4. Confirm the dashboard loads with the alert feed and counts panel visible.
5. Click `Open Camera`.
6. Walk into the camera frame and verify bounding boxes, labels, confidence, and persistent track IDs appear.
7. Cross the red virtual fence line.
8. Confirm a `VIRTUAL_FENCE_CROSS` alert appears, the alarm beep plays, and the red banner flashes.
9. Walk into the amber restricted-zone polygon.
10. Confirm a `RESTRICTED_ZONE_ENTRY` alert appears with the same alarm and banner behavior.
11. Open the event evidence image from the alert feed or `/evidence/<filename>.jpg`.
12. Verify the evidence crop includes the timestamp and configured location label burned into the image.
13. Click `Confirm` or `Reject` on an alert and verify the alert state updates in the dashboard.
14. Click `Close Camera` and verify the video panel hides. The counts and alerts continue polling independently.

`NEW_OBJECT_DETECTED` alerts are informational: they appear in the feed when a new track ID first appears, but they do not trigger the alarm.

## Multi-camera setup and demo

IBVAP supports multi-camera surveillance using USB webcams, RTSP/HTTP network streams, or smartphone cameras running video stream apps.

### 1. Using Phones as Cameras
- Install **IP Webcam** (Android) or similar MJPEG/RTSP stream app.
- Connect your phone and PC to the **same Wi-Fi hotspot** or local network.
- Start the server in the app; it will display an address such as `http://192.168.43.5:8080`.
- Verify the video stream by opening `http://<ip>:8080/video` in your desktop web browser first.

### 2. Configure `cameras.json`
Copy the example camera configuration and customize your camera sources and boundaries:
```powershell
Copy-Item cameras.example.json cameras.json
```
Edit `cameras.json` to configure:
- `source`: phone stream URL (e.g. `http://192.168.43.5:8080/video`), webcam index (e.g. `0`), or video file path (`sample_data/clip1.mp4`).
- `fence`: normalized coordinates (`start: [x, y]`, `end: [x, y]`) and `alert_direction` (`"ANY"`, `"A_TO_B"`, `"B_TO_A"`).
- `zone`: list of normalized polygon points (`[[x, y], ...]`).

### 3. Check Camera Streams Before Demo
Verify that all configured camera feeds are accessible and returning frames:
```powershell
python scripts/check_cameras.py
```
Each camera will print a status line: `<camera_id> | OK | <resolution> | <source>`. If any camera fails, the script exits with code 1 so you can fix connections before presenting.

### 4. One-Click Launch (`run.bat`)
Run the entire platform (backend, frontend, and browser launch) with a single command:
```powershell
.\run.bat
```
To stop both services cleanly, run:
```powershell
.\stop.bat
```

### 5. Running Tests
Run the test suite to verify system integrity:
```powershell
pytest -q
```

### 6. Troubleshooting
- **Phone IP changed**: Mobile hotspot DHCP can reassign IP addresses if phones disconnect. Re-check the IP in the IP Webcam app and update `cameras.json`.
- **Firewall blocking streams**: Ensure Windows Defender Firewall allows incoming connections on ports 8000 (backend) and 8080 (frontend/IP Webcam).
- **Python version**: Use Python 3.13 only (`py -3.13 --version`). Python 3.14+ may lack prebuilt OpenCV/NumPy wheels.
- **Virtual environment not activated**: Always run `.\venv\Scripts\Activate.ps1` in PowerShell before running Python scripts or commands directly.

## API Reference

Base URL:

```text
http://localhost:8000
```

### `GET /health`

Returns backend health.

Parameters: none.

Example response:

```json
{
  "status": "ok"
}
```

### `GET /stats`

Returns current-frame active tracked object counts. These are not cumulative totals.

Parameters: none.

Example response:

```json
{
  "person": 1,
  "vehicle": 0,
  "animal": 1
}
```

### `GET /video_feed`

Streams the latest annotated detector frame as multipart MJPEG.

Parameters: none.

Response content type:

```text
multipart/x-mixed-replace; boundary=frame
```

Example browser usage:

```html
<img src="http://localhost:8000/video_feed" alt="Annotated live camera feed">
```

### `GET /events`

Returns stored events newest first.

Query parameters:

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `camera_id` | string | No | Filter by camera ID. |
| `event_type` | string | No | Filter by event type. |
| `start_time` | string | No | Return events with `timestamp >= start_time`. |
| `end_time` | string | No | Return events with `timestamp <= end_time`. |
| `limit` | integer | No | Maximum rows to return. Default `100`, minimum `1`, maximum `1000`. |

Example request:

```text
GET /events?event_type=VIRTUAL_FENCE_CROSS&limit=10
```

Example response:

```json
[
  {
    "event_id": "7b7246b6-8b7e-45c4-a4df-79fb948b7ed4",
    "camera_id": "cam_01",
    "timestamp": "2026-08-23T10:15:30.123456+00:00",
    "event_type": "VIRTUAL_FENCE_CROSS",
    "object_type": "person",
    "track_id": 4,
    "confidence": 0.87,
    "evidence_path": "C:\\Users\\user\\ibvap-sih2026\\evidence\\7b7246b6-8b7e-45c4-a4df-79fb948b7ed4.jpg",
    "ack_status": "pending"
  }
]
```

### `GET /events/{event_id}`

Returns one stored event by ID.

Path parameters:

| Parameter | Type | Description |
| --- | --- | --- |
| `event_id` | string | Event UUID. |

Example response:

```json
{
  "event_id": "7b7246b6-8b7e-45c4-a4df-79fb948b7ed4",
  "camera_id": "cam_01",
  "timestamp": "2026-08-23T10:15:30.123456+00:00",
  "event_type": "RESTRICTED_ZONE_ENTRY",
  "object_type": "vehicle",
  "track_id": 9,
  "confidence": 0.82,
  "evidence_path": "C:\\Users\\user\\ibvap-sih2026\\evidence\\7b7246b6-8b7e-45c4-a4df-79fb948b7ed4.jpg",
  "ack_status": "pending"
}
```

Missing events return `404` with:

```json
{
  "detail": "Event not found"
}
```

### `POST /events/{event_id}/ack`

Updates human review status for an event.

Path parameters:

| Parameter | Type | Description |
| --- | --- | --- |
| `event_id` | string | Event UUID. |

Request body:

```json
{
  "status": "confirmed"
}
```

Allowed `status` values:

```text
confirmed
rejected
```

Example response:

```json
{
  "event_id": "7b7246b6-8b7e-45c4-a4df-79fb948b7ed4",
  "camera_id": "cam_01",
  "timestamp": "2026-08-23T10:15:30.123456+00:00",
  "event_type": "VIRTUAL_FENCE_CROSS",
  "object_type": "person",
  "track_id": 4,
  "confidence": 0.87,
  "evidence_path": "C:\\Users\\user\\ibvap-sih2026\\evidence\\7b7246b6-8b7e-45c4-a4df-79fb948b7ed4.jpg",
  "ack_status": "confirmed"
}
```

Invalid status returns `400` with:

```json
{
  "detail": "Invalid ack status"
}
```

Missing events return `404` with:

```json
{
  "detail": "Event not found"
}
```

### `GET /evidence/{filename}`

Serves evidence images from the configured evidence directory through FastAPI static files.

Path parameters:

| Parameter | Type | Description |
| --- | --- | --- |
| `filename` | string | Evidence JPEG filename. |

Example:

```text
GET /evidence/7b7246b6-8b7e-45c4-a4df-79fb948b7ed4.jpg
```

Response:

```text
JPEG image bytes
```

## Project Structure

```text
ibvap-sih2026/
|-- .env.example              # Template for runtime configuration.
|-- .gitignore                # Ignores local env, DB, generated evidence, caches.
|-- README.md                 # Project documentation.
|-- PROGRESS.md               # Phase-by-phase development notes and test steps.
|-- requirements.txt          # Python dependency pins.
|-- yolov8n.pt                # Local YOLOv8n model weights when present.
|-- ibvap.db                  # Generated SQLite database when present.
|-- backend/
|   |-- __init__.py           # Backend package marker.
|   |-- config.py             # Environment loading and typed runtime settings.
|   |-- db.py                 # SQLite schema, inserts, queries, and ack updates.
|   |-- detector.py           # YOLO detection, ByteTrack tracking, frame loop.
|   |-- event_runtime.py      # Event orchestration, evidence saving, DB writes.
|   |-- events.py             # Fence, restricted-zone, and event-building logic.
|   |-- live_state.py         # Thread-safe latest-frame and counts buffer.
|   |-- main.py               # FastAPI app, routes, CORS, detector thread startup.
|   `-- utils/
|       |-- __init__.py       # Utility package marker.
|       |-- annotation.py     # Bounding boxes, labels, fence, zone, count overlay.
|       |-- detections.py     # YOLO result to tracked-detection adapter.
|       |-- evidence.py       # Evidence crop, timestamp, and location stamping.
|       `-- video_source.py   # Camera index and video-file source handling.
|-- frontend/
|   |-- index.html            # Dashboard markup.
|   |-- app.js                # Polling, camera toggle, alarm, ack workflow.
|   `-- style.css             # Dashboard styling.
|-- evidence/                 # Generated evidence JPEGs.
`-- sample_data/              # Optional local test videos.
```

## Configuration Reference

| Variable | What it controls | Default |
| --- | --- | --- |
| `IBVAP_MODEL_PATH` | YOLO model weights path. | `yolov8n.pt` |
| `IBVAP_CONFIDENCE_THRESHOLD` | Minimum confidence for detections. | `0.35` |
| `IBVAP_CLASS_FILTER` | COCO class IDs sent to YOLO tracking. | `0,2,3,5,7,14,15,16,17,18,19,20,21,22,23` |
| `IBVAP_VIDEO_SOURCE` | OpenCV video source: camera index or file path. | `0` |
| `IBVAP_FRAME_LOG_INTERVAL` | Number of processed frames between FPS logs. | `30` |
| `IBVAP_TRACKER_TYPE` | Ultralytics tracker configuration. | `bytetrack.yaml` |
| `IBVAP_CAMERA_ID` | Camera ID stored with each event. | `cam_01` |
| `IBVAP_LOCATION_LABEL` | Location text burned into evidence images. | `BOP West Gate - Camera 1` |
| `IBVAP_FENCE_START` | Virtual fence start point, normalized or pixel coordinates. | `0.0,0.5` |
| `IBVAP_FENCE_END` | Virtual fence end point, normalized or pixel coordinates. | `1.0,0.5` |
| `IBVAP_RESTRICTED_ZONE` | Semicolon-separated polygon points, normalized or pixel coordinates. | `0.62,0.18;0.95,0.18;0.95,0.82;0.62,0.82` |
| `IBVAP_EVENT_COOLDOWN_SECONDS` | Per-track cooldown for repeated fence and zone alerts. | `4.0` |
| `IBVAP_EVIDENCE_DIR` | Directory where evidence crops are saved. | `evidence` |
| `IBVAP_DATABASE_PATH` | SQLite database file path. | `ibvap.db` |
| `IBVAP_DISPLAY_ENABLED` | Enables an additional OpenCV preview window. | `false` in code, `true` in `.env.example` |

COCO class IDs enabled by default:

| Group | IDs | Classes |
| --- | --- | --- |
| Person | `0` | person |
| Vehicle | `2,3,5,7` | car, motorcycle, bus, truck |
| Animal | `14-23` | bird, cat, dog, horse, sheep, cow, elephant, bear, zebra, giraffe |

## Limitations / Future Work

- Tracker identity is not persistent after full occlusion or leaving the scene because ByteTrack maintains short-term frame-to-frame associations rather than long-term biometric or appearance re-identification.
- The current runtime is single-camera; multi-camera ingestion and per-camera stream selection are planned future work.
- Loitering or dwell-time detection is not implemented yet.
- Evidence images are generated locally and should remain git-ignored for privacy and repository size.

## Team / Hackathon Credit

- Team name: `Clanaura `
- Institution: `Rungta International Skills University`
- Smart India Hackathon problem statement ID: `SIH 26187`
- Project: `IBVAP - Intelligent Border Video Analytics Platform`
