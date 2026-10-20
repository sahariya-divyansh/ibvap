"""Configuration for the IBVAP detection prototype."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"

DEFAULT_MODEL_PATH = "yolov8n.pt"
DEFAULT_CONFIDENCE_THRESHOLD = 0.35
DEFAULT_CLASS_FILTER = (0, 2, 3, 5, 7, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23)
DEFAULT_VIDEO_SOURCE = "0"
DEFAULT_FRAME_LOG_INTERVAL = 30
DEFAULT_TRACKER_TYPE = "bytetrack.yaml"
DEFAULT_CAMERA_ID = "cam_01"
DEFAULT_LOCATION_LABEL = "BOP West Gate - Camera 1"
DEFAULT_FENCE_START = (0.0, 0.5)
DEFAULT_FENCE_END = (1.0, 0.5)
DEFAULT_RESTRICTED_ZONE = ((0.62, 0.18), (0.95, 0.18), (0.95, 0.82), (0.62, 0.82))
DEFAULT_EVENT_COOLDOWN_SECONDS = 4.0
DEFAULT_EVIDENCE_DIR = PROJECT_ROOT / "evidence"
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "ibvap.db"
DEFAULT_DISPLAY_ENABLED = False

WINDOW_NAME = "IBVAP Detection"
EXIT_KEY = "q"
BOX_COLOR = (0, 255, 0)
TEXT_COLOR = (255, 255, 255)
TEXT_BACKGROUND_COLOR = (0, 128, 0)
BOX_THICKNESS = 2
FENCE_COLOR = (0, 0, 255)
FENCE_THICKNESS = 2
ZONE_COLOR = (0, 180, 255)
ZONE_FILL_ALPHA = 0.24
TEXT_FONT = 0
TEXT_SCALE = 0.6
TEXT_THICKNESS = 2
TEXT_PADDING = 6


def _load_env_file(path: Path = ENV_FILE) -> None:
    """Load simple KEY=VALUE pairs from a local .env file if present."""
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _parse_class_filter(raw_value: str) -> tuple[int, ...]:
    """Parse a comma-separated list of COCO class ids."""
    return tuple(int(item.strip()) for item in raw_value.split(",") if item.strip())


def _parse_point(raw_value: str) -> tuple[float, float]:
    """Parse an x,y point from configuration."""
    parts = [part.strip() for part in raw_value.split(",") if part.strip()]
    if len(parts) != 2:
        raise ValueError(f"Expected point as x,y, got: {raw_value}")
    return (float(parts[0]), float(parts[1]))


def _parse_polygon(raw_value: str) -> tuple[tuple[float, float], ...]:
    """Parse semicolon-separated x,y points from configuration."""
    points = tuple(
        _parse_point(point.strip())
        for point in raw_value.split(";")
        if point.strip()
    )
    if len(points) < 3:
        raise ValueError(f"Expected polygon with at least 3 points, got: {raw_value}")
    return points


def _parse_project_path(raw_value: str) -> Path:
    """Parse a path, resolving relative paths from the project root."""
    path = Path(raw_value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


DEFAULT_GEMINI_ENABLED = True
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"  # check which model your key can use
DEFAULT_GEMINI_RPM = 8.0
DEFAULT_GEMINI_TIMEOUT = 20.0
DEFAULT_GEMINI_MAX_IMAGE_PX = 640


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the detector."""

    model_path: str
    confidence_threshold: float
    class_filter: tuple[int, ...]
    video_source: str
    frame_log_interval: int
    tracker_type: str
    camera_id: str
    location_label: str
    fence_start: tuple[float, float]
    fence_end: tuple[float, float]
    restricted_zone: tuple[tuple[float, float], ...]
    event_cooldown_seconds: float
    evidence_dir: Path
    database_path: Path
    display_enabled: bool
    gemini_api_key: str | None = None
    gemini_enabled: bool = False
    gemini_model: str = DEFAULT_GEMINI_MODEL
    gemini_rpm: float = DEFAULT_GEMINI_RPM
    gemini_timeout: float = DEFAULT_GEMINI_TIMEOUT
    gemini_max_image_px: int = DEFAULT_GEMINI_MAX_IMAGE_PX


def get_settings() -> Settings:
    """Build settings from .env/environment values with project defaults."""
    _load_env_file()

    gemini_api_key = os.getenv("GEMINI_API_KEY")
    if os.getenv("IBVAP_GEMINI_ENABLED") is not None:
        gemini_enabled = _parse_bool(os.getenv("IBVAP_GEMINI_ENABLED", ""))
    else:
        gemini_enabled = bool(gemini_api_key)

    gemini_model = os.getenv("IBVAP_GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    gemini_rpm = float(os.getenv("IBVAP_GEMINI_RPM", str(DEFAULT_GEMINI_RPM)))
    gemini_timeout = float(os.getenv("IBVAP_GEMINI_TIMEOUT", str(DEFAULT_GEMINI_TIMEOUT)))
    gemini_max_image_px = int(
        os.getenv("IBVAP_GEMINI_MAX_IMAGE_PX", str(DEFAULT_GEMINI_MAX_IMAGE_PX))
    )

    return Settings(
        model_path=os.getenv("IBVAP_MODEL_PATH", DEFAULT_MODEL_PATH),
        confidence_threshold=float(
            os.getenv("IBVAP_CONFIDENCE_THRESHOLD", str(DEFAULT_CONFIDENCE_THRESHOLD))
        ),
        class_filter=_parse_class_filter(
            os.getenv(
                "IBVAP_CLASS_FILTER",
                ",".join(str(class_id) for class_id in DEFAULT_CLASS_FILTER),
            )
        ),
        video_source=os.getenv("IBVAP_VIDEO_SOURCE", DEFAULT_VIDEO_SOURCE),
        frame_log_interval=int(
            os.getenv("IBVAP_FRAME_LOG_INTERVAL", str(DEFAULT_FRAME_LOG_INTERVAL))
        ),
        tracker_type=os.getenv("IBVAP_TRACKER_TYPE", DEFAULT_TRACKER_TYPE),
        camera_id=os.getenv("IBVAP_CAMERA_ID", DEFAULT_CAMERA_ID),
        location_label=os.getenv("IBVAP_LOCATION_LABEL", DEFAULT_LOCATION_LABEL),
        fence_start=_parse_point(
            os.getenv(
                "IBVAP_FENCE_START",
                ",".join(str(value) for value in DEFAULT_FENCE_START),
            )
        ),
        fence_end=_parse_point(
            os.getenv(
                "IBVAP_FENCE_END",
                ",".join(str(value) for value in DEFAULT_FENCE_END),
            )
        ),
        restricted_zone=_parse_polygon(
            os.getenv(
                "IBVAP_RESTRICTED_ZONE",
                ";".join(
                    ",".join(str(value) for value in point)
                    for point in DEFAULT_RESTRICTED_ZONE
                ),
            )
        ),
        event_cooldown_seconds=float(
            os.getenv(
                "IBVAP_EVENT_COOLDOWN_SECONDS",
                str(DEFAULT_EVENT_COOLDOWN_SECONDS),
            )
        ),
        evidence_dir=_parse_project_path(
            os.getenv("IBVAP_EVIDENCE_DIR", str(DEFAULT_EVIDENCE_DIR))
        ),
        database_path=_parse_project_path(
            os.getenv("IBVAP_DATABASE_PATH", str(DEFAULT_DATABASE_PATH))
        ),
        display_enabled=_parse_bool(
            os.getenv("IBVAP_DISPLAY_ENABLED", str(DEFAULT_DISPLAY_ENABLED))
        ),
        gemini_api_key=gemini_api_key,
        gemini_enabled=gemini_enabled,
        gemini_model=gemini_model,
        gemini_rpm=gemini_rpm,
        gemini_timeout=gemini_timeout,
        gemini_max_image_px=gemini_max_image_px,
    )


def _parse_bool(raw_value: str) -> bool:
    """Parse common environment boolean strings."""
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Expected boolean value, got: {raw_value}")
