"""Manager coordinating multi-camera workers, threads, and lifecycle."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from backend.camera_config import CameraConfig
from backend.camera_worker import CameraWorker
from backend.config import Settings
from backend.live_state import LiveDetectorState

LOGGER = logging.getLogger(__name__)


class CameraManager:
    """Orchestrate multiple camera workers, CPU thread allocations, and lifecycle."""

    def __init__(
        self,
        cameras: list[CameraConfig],
        settings: Settings,
        *,
        live_state: LiveDetectorState | None = None,
        worker_factory: Callable[..., Any] = CameraWorker,
    ) -> None:
        """Create camera workers for all registered camera configs."""
        self.cameras = list(cameras)
        self.settings = settings
        self.live_state = live_state
        self._worker_factory = worker_factory
        self._workers: dict[str, Any] = {}

        is_single = len(self.cameras) == 1
        for cam in self.cameras:
            self._workers[cam.camera_id] = self._worker_factory(
                cam,
                settings,
                live_state=live_state,
                is_single_camera=is_single,
            )

    def start_all(self) -> None:
        """Allocate per-camera torch threads and start all worker pipelines."""
        num_cameras = max(1, len(self.cameras))
        cpu_count = os.cpu_count() or 2
        num_threads = max(1, cpu_count // num_cameras)

        try:
            import torch

            torch.set_num_threads(num_threads)
            LOGGER.info(
                "Allocated %d PyTorch thread(s) per camera (%d total camera(s))",
                num_threads,
                num_cameras,
            )
        except (ImportError, RuntimeError, AttributeError):
            pass

        for worker in self._workers.values():
            worker.start()

        LOGGER.info(
            "Started %d camera worker(s): %s",
            len(self._workers),
            list(self._workers.keys()),
        )

    def stop_all(self, timeout: float = 2.0) -> None:
        """Stop all camera workers cleanly."""
        for worker in self._workers.values():
            worker.stop(timeout=timeout)
        LOGGER.info("Stopped all camera workers")

    def get(self, camera_id: str) -> Any | None:
        """Retrieve worker instance for a camera by ID."""
        return self._workers.get(camera_id)

    def ids(self) -> list[str]:
        """Return list of managed camera IDs."""
        return list(self._workers.keys())

    def __len__(self) -> int:
        """Return number of managed cameras."""
        return len(self._workers)
