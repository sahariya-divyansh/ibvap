"""Health check script verifying camera stream sources before demo or production runs."""

from __future__ import annotations

import logging
import sys
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

import cv2

# Ensure repo root is on sys.path when invoked directly as a script
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.camera_config import load_cameras
from backend.utils.video_source import parse_video_source

LOGGER = logging.getLogger(__name__)


def sanitize_source(source: str | int) -> str:
    """Sanitize URL sources to remove sensitive credentials while preserving target."""
    if isinstance(source, int):
        return str(source)
    source_str = str(source)
    if "://" in source_str:
        try:
            parsed = urllib.parse.urlsplit(source_str)
            if parsed.username or parsed.password:
                netloc = parsed.hostname or ""
                if parsed.port:
                    netloc += f":{parsed.port}"
                return urllib.parse.urlunsplit(
                    (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
                )
        except ValueError:
            return source_str
    return source_str


def check_source(
    capture_factory: Callable[[str | int], Any],
    source: str | int,
    timeout: float = 5.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, str]:
    """Open a video source, attempt to read one frame within timeout, and return (status, resolution)."""
    cap = None
    try:
        cap = capture_factory(source)
        if not cap or not hasattr(cap, "isOpened") or not cap.isOpened():
            return False, "N/A"

        deadline = clock() + timeout
        while clock() < deadline:
            ret, frame = cap.read()
            if ret and frame is not None and hasattr(frame, "shape") and len(frame.shape) >= 2:
                h, w = frame.shape[:2]
                return True, f"{w}x{h}"
            sleep(0.05)
        return False, "N/A"
    except (cv2.error, OSError, RuntimeError, AttributeError, ValueError):
        return False, "N/A"
    finally:
        if cap is not None and hasattr(cap, "release"):
            try:
                cap.release()
            except (cv2.error, OSError, RuntimeError, AttributeError):
                LOGGER.debug("Failed to release capture resource")


def default_capture_factory(source: str | int) -> Any:
    """Open video source with OpenCV."""
    target: int | str
    if isinstance(source, int):
        target = source
    else:
        target = parse_video_source(source)
    cap = cv2.VideoCapture(target)
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except (cv2.error, AttributeError, RuntimeError):
        LOGGER.debug("Failed to set buffer size on capture")
    return cap


def main() -> int:
    """Verify all cameras defined in cameras.json or legacy config."""
    cameras = load_cameras()
    all_ok = True
    for cam in cameras:
        ok, resolution = check_source(default_capture_factory, cam.source, timeout=5.0)
        status_str = "OK" if ok else "FAIL"
        display_source = sanitize_source(cam.source)
        print(f"{cam.camera_id} | {status_str} | {resolution} | {display_source}")
        if not ok:
            all_ok = False
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
