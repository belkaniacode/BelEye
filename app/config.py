from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from .paths import cameras_file

log = logging.getLogger(__name__)


@dataclass
class CameraConfig:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "Camera"
    host: str = "127.0.0.1"
    port: int = 554
    username: str = ""
    path: str = "/"
    transport: str = "tcp"  # "tcp" or "udp"

    @classmethod
    def from_dict(cls, data: dict) -> "CameraConfig":
        return cls(
            id=data.get("id") or uuid.uuid4().hex,
            name=data.get("name", "Camera"),
            host=data.get("host", "127.0.0.1"),
            port=int(data.get("port", 554)),
            username=data.get("username", ""),
            path=data.get("path", "/"),
            transport=data.get("transport", "tcp"),
        )

    def to_dict(self) -> dict:
        return asdict(self)


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def load_cameras() -> list[CameraConfig]:
    path = cameras_file()
    if not path.exists():
        log.info("No cameras file at %s; starting empty.", path)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Failed to read cameras file: %s", exc)
        return []
    if not isinstance(data, list):
        log.warning("Cameras file malformed (not a list); ignoring.")
        return []
    return [CameraConfig.from_dict(item) for item in data if isinstance(item, dict)]


def save_cameras(cameras: Iterable[CameraConfig]) -> None:
    path = cameras_file()
    serialized = json.dumps([c.to_dict() for c in cameras], indent=2, ensure_ascii=False)
    _atomic_write(path, serialized)
    log.info("Saved %d cameras to %s", len(list(cameras)) if hasattr(cameras, "__len__") else -1, path)


def add_camera(cameras: list[CameraConfig], camera: CameraConfig) -> list[CameraConfig]:
    cameras.append(camera)
    save_cameras(cameras)
    return cameras


def update_camera(cameras: list[CameraConfig], camera: CameraConfig) -> list[CameraConfig]:
    for idx, existing in enumerate(cameras):
        if existing.id == camera.id:
            cameras[idx] = camera
            break
    else:
        cameras.append(camera)
    save_cameras(cameras)
    return cameras


def delete_camera(cameras: list[CameraConfig], camera_id: str) -> list[CameraConfig]:
    cameras = [c for c in cameras if c.id != camera_id]
    save_cameras(cameras)
    return cameras
