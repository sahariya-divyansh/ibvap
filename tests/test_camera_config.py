"""Unit tests for backend.camera_config registry loader and geometry resolver."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.camera_config import (
    AnalyticsFlags,
    CameraConfig,
    CameraConfigError,
    FenceConfig,
    ResolvedGeometry,
    load_cameras,
    resolve_geometry,
    save_cameras,
)
from backend.events import _scale_point


def _make_sample_camera(
    camera_id: str = "cam_01",
    location: str = "BOP West Gate",
    source: int | str = 0,
    fence_start: list[float] | None = None,
    fence_end: list[float] | None = None,
    alert_direction: str = "ANY",
    zone: list[list[float]] | None = None,
    **kwargs: object,
) -> CameraConfig:
    """Helper to construct a valid CameraConfig for testing."""
    return CameraConfig(
        camera_id=camera_id,
        location=location,
        source=source,
        fence=FenceConfig(
            start=fence_start or [0.0, 0.5],
            end=fence_end or [1.0, 0.5],
            alert_direction=alert_direction,  # type: ignore[arg-type]
        ),
        zone=zone if zone is not None else [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]],
        **kwargs,  # type: ignore[arg-type]
    )


def test_valid_multi_camera_file_loads_and_defaults_applied(tmp_path: Path) -> None:
    """A valid JSON file with multiple cameras loads and defaults are applied."""
    data = {
        "cameras": [
            {
                "camera_id": "cam_01",
                "location": "BOP West Gate",
                "source": "http://192.168.43.5:8080/video",
                "loop": False,
                "inference_stride": 2,
                "target_width": 640,
                "fence": {
                    "start": [0.0, 0.5],
                    "end": [1.0, 0.5],
                    "alert_direction": "A_TO_B",
                },
                "zone": [[0.62, 0.18], [0.95, 0.18], [0.95, 0.82], [0.62, 0.82]],
                "loiter_seconds": 15.0,
                "cooldown_seconds": 5.0,
                "analytics": {
                    "loitering": True,
                    "health": True,
                    "night": False,
                    "trails": True,
                    "clips": False,
                    "sequence": True,
                },
            },
            {
                "camera_id": "cam_02",
                "location": "Perimeter North",
                "source": "0",
                "fence": {
                    "start": [0.0, 0.6],
                    "end": [1.0, 0.6],
                },
            },
        ]
    }
    cfg_path = tmp_path / "cameras.json"
    cfg_path.write_text(json.dumps(data), encoding="utf-8")

    cams = load_cameras(cfg_path)
    assert len(cams) == 2

    cam1 = cams[0]
    assert cam1.camera_id == "cam_01"
    assert cam1.location == "BOP West Gate"
    assert cam1.source == "http://192.168.43.5:8080/video"
    assert cam1.loop is False
    assert cam1.inference_stride == 2
    assert cam1.target_width == 640
    assert cam1.fence.alert_direction == "A_TO_B"
    assert len(cam1.zone) == 4
    assert cam1.loiter_seconds == 15.0
    assert cam1.cooldown_seconds == 5.0
    assert cam1.analytics.loitering is True
    assert cam1.analytics.sequence is True

    # Check defaults on cam2
    cam2 = cams[1]
    assert cam2.camera_id == "cam_02"
    assert cam2.source == 0  # digit string "0" converted to int 0
    assert cam2.loop is False
    assert cam2.inference_stride == 1
    assert cam2.target_width is None
    assert cam2.fence.alert_direction == "ANY"
    assert cam2.zone == []
    assert cam2.loiter_seconds == 10.0
    assert cam2.cooldown_seconds == 4.0
    assert cam2.analytics.loitering is False
    assert cam2.analytics.health is False
    assert cam2.analytics.night is False
    assert cam2.analytics.trails is True
    assert cam2.analytics.clips is False
    assert cam2.analytics.sequence is False


def test_unknown_key_rejected_with_message_naming_it(tmp_path: Path) -> None:
    """Unknown keys fail loudly with human-readable error naming camera and key."""
    data = {
        "cameras": [
            {
                "camera_id": "cam_01",
                "location": "Gate",
                "source": "0",
                "fence": {"start": [0.0, 0.5], "end": [1.0, 0.5]},
                "unrecognized_option": 123,
            }
        ]
    }
    cfg_path = tmp_path / "cameras.json"
    cfg_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(CameraConfigError) as exc_info:
        load_cameras(cfg_path)

    err_msg = str(exc_info.value)
    assert "cam_01" in err_msg
    assert "unrecognized_option" in err_msg


def test_duplicate_camera_id_rejected(tmp_path: Path) -> None:
    """Duplicate camera_id across cameras must be rejected."""
    data = {
        "cameras": [
            {
                "camera_id": "cam_dupe",
                "location": "Gate 1",
                "source": "0",
                "fence": {"start": [0.0, 0.5], "end": [1.0, 0.5]},
            },
            {
                "camera_id": "cam_dupe",
                "location": "Gate 2",
                "source": "1",
                "fence": {"start": [0.0, 0.5], "end": [1.0, 0.5]},
            },
        ]
    }
    cfg_path = tmp_path / "cameras.json"
    cfg_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(CameraConfigError) as exc_info:
        load_cameras(cfg_path)

    assert "cam_dupe" in str(exc_info.value)


def test_zone_with_two_points_rejected(tmp_path: Path) -> None:
    """Zone with 2 points must be rejected (must be empty or have >= 3 points)."""
    data = {
        "cameras": [
            {
                "camera_id": "cam_01",
                "location": "Gate",
                "source": "0",
                "fence": {"start": [0.0, 0.5], "end": [1.0, 0.5]},
                "zone": [[0.1, 0.1], [0.2, 0.2]],
            }
        ]
    }
    cfg_path = tmp_path / "cameras.json"
    cfg_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(CameraConfigError) as exc_info:
        load_cameras(cfg_path)

    assert "zone" in str(exc_info.value).lower()
    assert "cam_01" in str(exc_info.value)


def test_empty_cameras_list_rejected(tmp_path: Path) -> None:
    """Empty cameras list must be rejected."""
    data = {"cameras": []}
    cfg_path = tmp_path / "cameras.json"
    cfg_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(CameraConfigError) as exc_info:
        load_cameras(cfg_path)

    assert "cameras" in str(exc_info.value).lower()


def test_bad_alert_direction_rejected(tmp_path: Path) -> None:
    """Invalid alert_direction value must be rejected."""
    data = {
        "cameras": [
            {
                "camera_id": "cam_01",
                "location": "Gate",
                "source": "0",
                "fence": {
                    "start": [0.0, 0.5],
                    "end": [1.0, 0.5],
                    "alert_direction": "SIDEWAYS",
                },
            }
        ]
    }
    cfg_path = tmp_path / "cameras.json"
    cfg_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(CameraConfigError) as exc_info:
        load_cameras(cfg_path)

    err_msg = str(exc_info.value)
    assert "alert_direction" in err_msg
    assert "cam_01" in err_msg


def test_inference_stride_zero_rejected(tmp_path: Path) -> None:
    """Inference stride 0 must be rejected (must be >= 1)."""
    data = {
        "cameras": [
            {
                "camera_id": "cam_01",
                "location": "Gate",
                "source": "0",
                "inference_stride": 0,
                "fence": {"start": [0.0, 0.5], "end": [1.0, 0.5]},
            }
        ]
    }
    cfg_path = tmp_path / "cameras.json"
    cfg_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(CameraConfigError) as exc_info:
        load_cameras(cfg_path)

    assert "inference_stride" in str(exc_info.value)
    assert "cam_01" in str(exc_info.value)


def test_invalid_json_raises_camera_config_error(tmp_path: Path) -> None:
    """Invalid JSON syntax raises CameraConfigError, not JSONDecodeError."""
    cfg_path = tmp_path / "corrupt_cameras.json"
    cfg_path.write_text('{"cameras": [ {broken json', encoding="utf-8")

    with pytest.raises(CameraConfigError) as exc_info:
        load_cameras(cfg_path)

    assert not isinstance(exc_info.value, json.JSONDecodeError)
    assert "Invalid JSON" in str(exc_info.value)


def test_missing_file_falls_back_to_legacy_camera_from_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing config file produces exactly one camera with values from Settings."""
    missing_file = tmp_path / "missing_cameras.json"
    monkeypatch.setenv("IBVAP_CAMERAS_FILE", str(missing_file))
    monkeypatch.setenv("IBVAP_CAMERA_ID", "legacy_cam_42")
    monkeypatch.setenv("IBVAP_LOCATION_LABEL", "BOP Outpost East")
    monkeypatch.setenv("IBVAP_VIDEO_SOURCE", "0")
    monkeypatch.setenv("IBVAP_FENCE_START", "0.1,0.4")
    monkeypatch.setenv("IBVAP_FENCE_END", "0.9,0.4")
    monkeypatch.setenv(
        "IBVAP_RESTRICTED_ZONE",
        "0.2,0.2;0.8,0.2;0.8,0.8;0.2,0.8",
    )
    monkeypatch.setenv("IBVAP_EVENT_COOLDOWN_SECONDS", "8.5")

    cams = load_cameras()
    assert len(cams) == 1
    cam = cams[0]
    assert cam.camera_id == "legacy_cam_42"
    assert cam.location == "BOP Outpost East"
    assert cam.source == 0
    assert cam.fence.start == [0.1, 0.4]
    assert cam.fence.end == [0.9, 0.4]
    assert cam.zone == [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]
    assert cam.cooldown_seconds == 8.5
    assert cam.fence.alert_direction == "ANY"


def test_source_handling_digit_to_int_and_string_preserved(tmp_path: Path) -> None:
    """Digit string '0' becomes int 0; file paths and URLs remain str."""
    cam_digit = _make_sample_camera(camera_id="c1", source="0")
    assert cam_digit.source == 0
    assert isinstance(cam_digit.source, int)

    cam_int = _make_sample_camera(camera_id="c2", source=1)
    assert cam_int.source == 1
    assert isinstance(cam_int.source, int)

    cam_path = _make_sample_camera(camera_id="c3", source="sample_data/clip1.mp4")
    assert cam_path.source == "sample_data/clip1.mp4"
    assert isinstance(cam_path.source, str)

    cam_url = _make_sample_camera(camera_id="c4", source="http://192.168.1.10:8080/video")
    assert cam_url.source == "http://192.168.1.10:8080/video"
    assert isinstance(cam_url.source, str)


def test_resolve_geometry_normalized_scaled() -> None:
    """Normalized coordinates in [0, 1] are scaled by frame dimensions."""
    cam = _make_sample_camera(
        fence_start=[0.0, 0.5],
        fence_end=[1.0, 0.5],
        zone=[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]],
    )
    resolved = resolve_geometry(cam, frame_w=640, frame_h=480)
    assert isinstance(resolved, ResolvedGeometry)
    assert resolved.fence_start == (0.0, 240.0)
    assert resolved.fence_end == (640.0, 240.0)
    assert resolved.zone == [(0.0, 0.0), (640.0, 0.0), (640.0, 480.0)]


def test_resolve_geometry_pixel_unchanged() -> None:
    """Pixel coordinates (> 1) remain unchanged."""
    cam = _make_sample_camera(
        fence_start=[100.0, 200.0],
        fence_end=[500.0, 200.0],
        zone=[[10.0, 20.0], [30.0, 40.0], [50.0, 60.0]],
    )
    resolved = resolve_geometry(cam, frame_w=640, frame_h=480)
    assert resolved.fence_start == (100.0, 200.0)
    assert resolved.fence_end == (500.0, 200.0)
    assert resolved.zone == [(10.0, 20.0), (30.0, 40.0), (50.0, 60.0)]


def test_resolve_geometry_mixed_edge_case() -> None:
    """Coordinates where one dimension <= 1 and the other > 1 match existing behaviour."""
    # Existing _scale_point only scales if 0 <= x <= 1 and 0 <= y <= 1
    mixed_fence_start = [0.5, 200.0]
    mixed_fence_end = [500.0, 0.5]
    mixed_zone = [[0.5, 0.5], [100.0, 200.0], [0.5, 200.0]]

    cam = _make_sample_camera(
        fence_start=mixed_fence_start,
        fence_end=mixed_fence_end,
        zone=mixed_zone,
    )
    resolved = resolve_geometry(cam, frame_w=640, frame_h=480)

    # Point with y > 1 is not scaled by _scale_point
    expected_start = _scale_point((0.5, 200.0), 640, 480)
    expected_end = _scale_point((500.0, 0.5), 640, 480)
    expected_zone = [_scale_point((pt[0], pt[1]), 640, 480) for pt in mixed_zone]

    assert resolved.fence_start == expected_start
    assert resolved.fence_end == expected_end
    assert resolved.zone == expected_zone
    assert resolved.fence_start == (0.5, 200.0)
    assert resolved.fence_end == (500.0, 0.5)
    assert resolved.zone[0] == (320.0, 240.0)
    assert resolved.zone[1] == (100.0, 200.0)
    assert resolved.zone[2] == (0.5, 200.0)


def test_save_cameras_then_load_cameras_round_trips_equal(tmp_path: Path) -> None:
    """save_cameras then load_cameras round-trips equal with no temp files left behind."""
    cams = [
        _make_sample_camera(
            camera_id="cam_01",
            location="North Gate",
            source=0,
            inference_stride=2,
            target_width=640,
            loiter_seconds=12.0,
            cooldown_seconds=6.0,
            analytics=AnalyticsFlags(loitering=True, night=True),
        ),
        _make_sample_camera(
            camera_id="cam_02",
            location="South Gate",
            source="sample_data/clip1.mp4",
            loop=True,
            zone=[],
        ),
    ]

    target_path = tmp_path / "output_cameras.json"
    save_cameras(cams, target_path)

    # Verify no temp files remain in target directory
    remaining_files = list(tmp_path.iterdir())
    assert remaining_files == [target_path]

    # Verify loaded cameras equal saved cameras
    loaded = load_cameras(target_path)
    assert loaded == cams


def test_load_cameras_example_file() -> None:
    """Verify that the repository's cameras.example.json loads cleanly."""
    example_path = Path("cameras.example.json")
    assert example_path.is_file()
    cams = load_cameras(example_path)
    assert len(cams) == 3
    assert [c.camera_id for c in cams] == ["cam_01", "cam_02", "cam_03"]
