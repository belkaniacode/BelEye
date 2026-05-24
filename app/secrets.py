from __future__ import annotations

import logging

import keyring
from keyring.errors import KeyringError

SERVICE = "beleye"

log = logging.getLogger(__name__)

# [FIX] In-process cache. Keyring backends on Linux (SecretService over dbus
# under Wayland especially) can take 100-400 ms per call. set_cameras() asks
# for every camera's password twice per reload → noticeable UI hitch. Cache
# in memory; invalidate on set/delete.
_cache: dict[str, str] = {}


def set_password(camera_id: str, password: str) -> None:
    pwd = password or ""
    try:
        keyring.set_password(SERVICE, camera_id, pwd)
        _cache[camera_id] = pwd
    except KeyringError as exc:
        log.error("Failed to store password for %s: %s", camera_id, exc)
        raise


def get_password(camera_id: str) -> str:
    if camera_id in _cache:
        return _cache[camera_id]
    try:
        pwd = keyring.get_password(SERVICE, camera_id) or ""
    except KeyringError as exc:
        log.error("Failed to read password for %s: %s", camera_id, exc)
        pwd = ""
    _cache[camera_id] = pwd
    return pwd


def delete_password(camera_id: str) -> None:
    _cache.pop(camera_id, None)
    try:
        keyring.delete_password(SERVICE, camera_id)
    except KeyringError:
        pass
