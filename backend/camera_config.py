"""Camera registry configuration, loader, and geometry resolution."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
)

from backend.config import get_settings
from backend.events import _scale_point

LOGGER = logging.getLogger(__name__)


class CameraConfigError(Exception):
    """Raised when camera configuration is invalid, corrupted, or unreadable."""

    @classmethod
    def from_validation_error(
        cls,
        exc: ValidationError,
        raw_data: object | None = None,
    ) -> CameraConfigError:
        """Convert a pydantic ValidationError into a human-readable CameraConfigError."""
        return cls(_format_validation_error(exc, raw_data))


def _format_validation_error(
    err: ValidationError,
    raw_data: object | None = None,
) -> str:
    """Format ValidationError items into human-readable messages naming camera and key."""
    messages: list[str] = []
    for error in err.errors():
        loc = error.get("loc", ())
        msg = error.get("msg", "")

        camera_name: str | None = None
        key_parts: list[str] = []

        if loc and loc[0] == "cameras" and len(loc) > 1 and isinstance(loc[1], int):
            idx = loc[1]
            if isinstance(raw_data, dict) and "cameras" in raw_data:
                cams = raw_data.get("cameras")
                if isinstance(cams, list) and 0 <= idx < len(cams) and isinstance(cams[idx], dict):
                    camera_name = cams[idx].get("camera_id")
            if not camera_name:
                camera_name = f"index {idx}"
            key_parts = [str(p) for p in loc[2:]]
        else:
            if isinstance(raw_data, dict) and "camera_id" in raw_data:
                camera_name = str(raw_data.get("camera_id"))
            key_parts = [str(p) for p in loc]

        key_str = ".".join(key_parts)
        if camera_name and key_str:
            messages.append(f"Camera '{camera_name}', key '{key_str}': {msg}")
        elif camera_name:
            messages.append(f"Camera '{camera_name}': {msg}")
        elif key_str:
            messages.append(f"Key '{key_str}': {msg}")
        else:
            messages.append(msg)

    return "; ".join(messages) if messages else str(err)


class FenceConfig(BaseModel):
    """Virtual fence definition with endpoints and alert direction."""

    model_config = ConfigDict(extra="forbid")

    start: list[float]
    end: list[float]
    alert_direction: Literal["ANY", "A_TO_B", "B_TO_A"] = "ANY"

    @field_validator("start", "end")
    @classmethod
    def _validate_coords(cls, v: list[float], info: ValidationInfo) -> list[float]:
        if len(v) != 2:
            raise ValueError(
                f"'{info.field_name}' must have exactly 2 coordinates [x, y], got {len(v)}"
            )
        return v


class AnalyticsFlags(BaseModel):
    """Feature flags for per-camera analytics modules."""

    model_config = ConfigDict(extra="forbid")

    loitering: bool = False
    health: bool = False
    night: bool = False
    trails: bool = True
    clips: bool = False
    sequence: bool = False


class CameraConfig(BaseModel):
    """Complete configuration for a single camera stream and its analytics."""

    model_config = ConfigDict(extra="forbid")

    camera_id: str = Field(min_length=1)
    location: str
    source: int | str
    loop: bool = False
    inference_stride: int = Field(default=1, ge=1)
    target_width: int | None = Field(default=None, gt=0)
    fence: FenceConfig
    zone: list[list[float]] = Field(default_factory=list)
    loiter_seconds: float = Field(default=10.0, gt=0)
    cooldown_seconds: float = Field(
        default_factory=lambda: get_settings().event_cooldown_seconds,
        gt=0,
    )
    analytics: AnalyticsFlags = Field(default_factory=AnalyticsFlags)

    @field_validator("camera_id")
    @classmethod
    def _validate_camera_id(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("camera_id must be a non-empty string")
        return trimmed

    @field_validator("source", mode="before")
    @classmethod
    def _validate_source(cls, v: object) -> int | str:
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            trimmed = v.strip()
            if trimmed.isdigit():
                return int(trimmed)
            return trimmed
        raise ValueError(f"Invalid video source: {v}")

    @field_validator("zone")
    @classmethod
    def _validate_zone(cls, v: list[list[float]]) -> list[list[float]]:
        if len(v) != 0 and len(v) < 3:
            raise ValueError(
                f"Zone must be empty or contain at least 3 points, got {len(v)}"
            )
        for i, pt in enumerate(v):
            if len(pt) != 2:
                raise ValueError(
                    f"Zone point at index {i} must have exactly 2 coordinates [x, y], got {len(pt)}"
                )
        return v


class CamerasFile(BaseModel):
    """Root structure of cameras.json registry file."""

    model_config = ConfigDict(extra="forbid")

    cameras: list[CameraConfig] = Field(min_length=1)

    @field_validator("cameras")
    @classmethod
    def _validate_cameras(cls, cameras: list[CameraConfig]) -> list[CameraConfig]:
        if not cameras:
            raise ValueError("Cameras list must not be empty")
        seen_ids: set[str] = set()
        for cam in cameras:
            if cam.camera_id in seen_ids:
                raise ValueError(f"Duplicate camera_id: '{cam.camera_id}'")
            seen_ids.add(cam.camera_id)
        return cameras


@dataclass(frozen=True)
class ResolvedGeometry:
    """Resolved pixel coordinates for fence endpoints and restricted zone polygon."""

    fence_start: tuple[float, float]
    fence_end: tuple[float, float]
    zone: list[tuple[float, float]]


def resolve_geometry(
    cam: CameraConfig,
    frame_w: int,
    frame_h: int,
) -> ResolvedGeometry:
    """Resolve normalized or pixel coordinates for fence and zone to pixel space.

    Coordinates in [0, 1] are treated as normalized and multiplied by frame dimensions;
    coordinates > 1 are treated as already in pixel space.
    """
    fence_start = _scale_point(
        (float(cam.fence.start[0]), float(cam.fence.start[1])),
        frame_w,
        frame_h,
    )
    fence_end = _scale_point(
        (float(cam.fence.end[0]), float(cam.fence.end[1])),
        frame_w,
        frame_h,
    )
    zone = [
        _scale_point((float(pt[0]), float(pt[1])), frame_w, frame_h)
        for pt in cam.zone
    ]
    return ResolvedGeometry(
        fence_start=fence_start,
        fence_end=fence_end,
        zone=zone,
    )


def _build_legacy_camera() -> CameraConfig:
    """Build a single CameraConfig from legacy Settings / environment variables."""
    settings = get_settings()
    LOGGER.info(
        "No cameras configuration file found; using legacy settings for camera '%s'",
        settings.camera_id,
    )
    return CameraConfig(
        camera_id=settings.camera_id,
        location=settings.location_label,
        source=settings.video_source,
        loop=False,
        inference_stride=1,
        target_width=None,
        fence=FenceConfig(
            start=[settings.fence_start[0], settings.fence_start[1]],
            end=[settings.fence_end[0], settings.fence_end[1]],
            alert_direction="ANY",
        ),
        zone=[[point[0], point[1]] for point in settings.restricted_zone],
        loiter_seconds=10.0,
        cooldown_seconds=settings.event_cooldown_seconds,
        analytics=AnalyticsFlags(),
    )


def load_cameras(path: str | Path | None = None) -> list[CameraConfig]:
    """Load camera configs from file or fall back to legacy environment settings.

    If path is None, uses IBVAP_CAMERAS_FILE env var if set, otherwise 'cameras.json'.
    If the file does not exist, synthesizes one camera from legacy Settings.
    If the file exists but has invalid JSON or schema, raises CameraConfigError.
    """
    if path is None:
        path = os.getenv("IBVAP_CAMERAS_FILE", "cameras.json")

    file_path = Path(path)
    if not file_path.is_file():
        return [_build_legacy_camera()]

    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CameraConfigError(
            f"Failed to read camera config file '{file_path}': {exc}"
        ) from exc

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise CameraConfigError(
            f"Invalid JSON in camera config file '{file_path}': {exc}"
        ) from exc

    try:
        cameras_file = CamerasFile.model_validate(data)
    except ValidationError as exc:
        raise CameraConfigError.from_validation_error(exc, data) from exc

    return list(cameras_file.cameras)


def save_cameras(
    cameras: list[CameraConfig] | CamerasFile,
    path: str | Path | None = None,
) -> None:
    """Write camera configuration atomically to disk with pretty-printed JSON."""
    if path is None:
        path = os.getenv("IBVAP_CAMERAS_FILE", "cameras.json")

    target_path = Path(path)

    try:
        if isinstance(cameras, CamerasFile):
            file_model = cameras
        else:
            file_model = CamerasFile(cameras=cameras)
    except ValidationError as exc:
        raise CameraConfigError.from_validation_error(exc) from exc

    parent_dir = target_path.parent
    if parent_dir != Path("") and not parent_dir.exists():
        parent_dir.mkdir(parents=True, exist_ok=True)

    json_str = file_model.model_dump_json(indent=2) + "\n"

    dir_for_temp = parent_dir if str(parent_dir) != "" else Path(".")
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=dir_for_temp,
            prefix=".cameras_",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(json_str)
            temp_file.flush()

        os.replace(temp_path, target_path)
    except Exception:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
        raise
