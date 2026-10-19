"""Unit tests for backend.camera_manager lifecycle and thread allocation."""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from backend.camera_config import CameraConfig, FenceConfig
from backend.camera_manager import CameraManager
from backend.config import get_settings


class FakeWorker:
    """Mock worker to verify manager lifecycle calls."""

    def __init__(self, cam: CameraConfig, settings: Any, **kwargs: Any) -> None:
        self.cam = cam
        self.settings = settings
        self.started = False
        self.stopped = False
        self.stop_timeout: float | None = None

    def start(self) -> None:
        self.started = True

    def stop(self, timeout: float = 2.0) -> None:
        self.stopped = True
        self.stop_timeout = timeout


def _make_dummy_cam(camera_id: str) -> CameraConfig:
    return CameraConfig(
        camera_id=camera_id,
        location="Gate",
        source=0,
        fence=FenceConfig(start=[0.0, 0.5], end=[1.0, 0.5]),
        zone=[[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]],
    )


def test_camera_manager_start_stop_lifecycle() -> None:
    """CameraManager initializes, starts, and stops all managed workers."""
    cams = [_make_dummy_cam(f"cam_{i}") for i in range(3)]
    settings = get_settings()

    manager = CameraManager(
        cameras=cams,
        settings=settings,
        worker_factory=FakeWorker,
    )

    assert manager.ids() == ["cam_0", "cam_1", "cam_2"]
    assert len(manager) == 3

    w0 = manager.get("cam_0")
    assert isinstance(w0, FakeWorker)
    assert manager.get("unknown_cam") is None

    # Start all
    manager.start_all()
    for cid in manager.ids():
        worker = manager.get(cid)
        assert worker is not None
        assert worker.started is True

    # Stop all
    manager.stop_all(timeout=1.5)
    for cid in manager.ids():
        worker = manager.get(cid)
        assert worker is not None
        assert worker.stopped is True
        assert worker.stop_timeout == 1.5


@pytest.mark.parametrize(
    ("cpu_count", "n_cams", "expected_threads"),
    [
        (8, 1, 8),
        (8, 2, 4),
        (8, 3, 2),
        (8, 10, 1),
        (None, 4, 1),  # fallback to 2 CPUs -> 2 // 4 = 0 -> min 1
    ],
)
def test_camera_manager_thread_count_math(
    monkeypatch: pytest.MonkeyPatch,
    cpu_count: int | None,
    n_cams: int,
    expected_threads: int,
) -> None:
    """Thread count per camera is max(1, cpu_count // num_cameras)."""
    monkeypatch.setattr("os.cpu_count", lambda: cpu_count)

    mock_torch = MagicMock()
    monkeypatch.setitem(sys.modules, "torch", mock_torch)

    cams = [_make_dummy_cam(f"cam_{i}") for i in range(n_cams)]
    manager = CameraManager(
        cameras=cams,
        settings=get_settings(),
        worker_factory=FakeWorker,
    )
    manager.start_all()

    mock_torch.set_num_threads.assert_called_with(expected_threads)
