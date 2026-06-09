"""Persistent config for NVRs (Xiongmai/DVRIP regs on port 34567)."""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from .paths import nvrs_file

log = logging.getLogger(__name__)


@dataclass
class NvrChannel:
    number: int = 1
    name: str = ""
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "NvrChannel":
        return cls(
            number=int(data.get("number", 1)),
            name=str(data.get("name", "")),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass
class NvrConfig:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "NVR"
    host: str = "192.168.0.60"
    port: int = 34567
    username: str = "admin"
    channels: list[NvrChannel] = field(default_factory=list)
    # [FIX perf] Live tiles request the sub stream by default — Main is heavy
    # and on 4+ channels saturates the NVR encoder + LAN, causing freezes.
    prefer_substream: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "NvrConfig":
        return cls(
            id=data.get("id") or uuid.uuid4().hex,
            name=data.get("name", "NVR"),
            host=data.get("host", "192.168.0.60"),
            port=int(data.get("port", 34567)),
            username=data.get("username", "admin"),
            channels=[NvrChannel.from_dict(c) for c in (data.get("channels") or [])],
            prefer_substream=bool(data.get("prefer_substream", True)),
        )

    def to_dict(self) -> dict:
        return asdict(self)


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def load_nvrs() -> list[NvrConfig]:
    path = nvrs_file()
    if not path.exists():
        log.info("[config] no nvrs file at %s; starting empty", path)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("[config] failed to read nvrs file: %s", exc)
        return []
    if not isinstance(data, list):
        log.warning("[config] nvrs file malformed (not a list); ignoring.")
        return []
    out = [NvrConfig.from_dict(item) for item in data if isinstance(item, dict)]
    log.info("[config] loaded %d nvrs", len(out))
    return out


def save_nvrs(nvrs: Iterable[NvrConfig]) -> None:
    nvrs = list(nvrs)
    serialized = json.dumps([n.to_dict() for n in nvrs], indent=2, ensure_ascii=False)
    _atomic_write(nvrs_file(), serialized)
    log.info("[config] saved %d nvrs", len(nvrs))


def add_nvr(nvrs: list[NvrConfig], nvr: NvrConfig) -> list[NvrConfig]:
    nvrs.append(nvr)
    save_nvrs(nvrs)
    return nvrs


def update_nvr(nvrs: list[NvrConfig], nvr: NvrConfig) -> list[NvrConfig]:
    for i, existing in enumerate(nvrs):
        if existing.id == nvr.id:
            nvrs[i] = nvr
            break
    else:
        nvrs.append(nvr)
    save_nvrs(nvrs)
    return nvrs


def delete_nvr(nvrs: list[NvrConfig], nvr_id: str) -> list[NvrConfig]:
    nvrs = [n for n in nvrs if n.id != nvr_id]
    save_nvrs(nvrs)
    return nvrs


def nvr_keyring_user(nvr_id: str) -> str:
    """Distinct keyring username so NVR creds don't collide with camera creds."""
    return f"nvr:{nvr_id}"
