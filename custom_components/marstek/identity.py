"""Validation and comparison of persistent Marstek device identities."""

from __future__ import annotations

import re

from homeassistant.helpers.device_registry import format_mac

CONF_DEVICE_INFO = "device_info"
LEGACY_ID_PLACEHOLDERS = {"unknown", "", "None"}


def normalize_mac(value: object) -> str | None:
    """Return a usable BLE MAC for comparison, or None for invalid identities."""
    if not isinstance(value, str):
        return None
    mac = format_mac(value.strip())
    if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
        return None
    if mac in {"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"}:
        return None
    return mac


def device_metadata(info: dict) -> dict:
    """Cache only device metadata, excluding transient discovery/connection data."""
    return {
        key: value
        for key in ("device", "ble_mac", "wifi_mac", "ver")
        if isinstance(value := info.get(key), (str, int)) and value != ""
    }
