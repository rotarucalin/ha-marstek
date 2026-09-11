"""Repair registry records created with transient or placeholder identities."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .identity import LEGACY_ID_PLACEHOLDERS, normalize_mac

_LOGGER = logging.getLogger(__name__)


def _exclusively_owned(device: dr.DeviceEntry, entry: ConfigEntry) -> bool:
    """Account for HA versions where devices can belong to several entries."""
    if hasattr(device, "config_entry_id"):
        return device.config_entry_id == entry.entry_id
    return device.config_entries == {entry.entry_id}


@callback
def async_repair_registry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device_id: str,
    device_info: dr.DeviceInfo,
) -> str:
    """Repair only this entry's entities, retaining existing valid records."""
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    normalized_id = normalize_mac(device_id)
    if normalized_id is None:
        raise ConfigEntryError(
            "Cannot repair Marstek registry without a valid identity"
        )

    migrations = []
    for entity in er.async_entries_for_config_entry(entities, entry.entry_id):
        if entity.platform != DOMAIN:
            continue
        prefix, separator, key = entity.unique_id.partition("_")
        if not separator or not key:
            continue
        if (
            prefix not in LEGACY_ID_PLACEHOLDERS
            and normalize_mac(prefix) != normalized_id
        ):
            continue
        unique_id = f"{device_id}_{key}"
        existing_id = entities.async_get_entity_id(entity.domain, DOMAIN, unique_id)
        if existing_id is not None:
            existing = entities.async_get(existing_id)
            if existing.config_entry_id != entry.entry_id:
                # Continuing setup would let platform registration claim the
                # other entry's entity. Abort before changing either registry.
                raise ConfigEntryError(
                    f"Cannot migrate {entity.entity_id}: {existing_id} belongs "
                    "to another config entry"
                )
        migrations.append((entity, unique_id))
    # If a real-MAC entity uses another spelling, keep it ahead of placeholders.
    migrations.sort(
        key=lambda item: item[0].unique_id.partition("_")[0] in LEGACY_ID_PLACEHOLDERS
    )
    entry_devices = dr.async_entries_for_config_entry(devices, entry.entry_id)
    matching_devices = [
        device
        for device in entry_devices
        if any(
            domain == DOMAIN and normalize_mac(identifier) == normalized_id
            for domain, identifier in device.identifiers
        )
    ]
    placeholder_devices = [
        device
        for device in entry_devices
        if device.identifiers
        and all(
            domain == DOMAIN and identifier in LEGACY_ID_PLACEHOLDERS
            for domain, identifier in device.identifiers
        )
    ]

    # Prefer the real device. If only a placeholder exists and its ownership is
    # unambiguous, promote it in place to preserve device-based automations.
    target = next(
        (
            device
            for device in matching_devices
            if (DOMAIN, device_id) in device.identifiers
        ),
        next(iter(matching_devices), None),
    )
    if target is None:
        target = next(
            (
                device
                for device in placeholder_devices
                if _exclusively_owned(device, entry)
            ),
            None,
        )
    if target is not None:
        devices.async_update_device(
            target.id,
            new_identifiers={item for item in target.identifiers if item[0] != DOMAIN}
            | {(DOMAIN, device_id)},
        )
    target = devices.async_get_or_create(config_entry_id=entry.entry_id, **device_info)

    for entity, unique_id in migrations:
        existing_id = entities.async_get_entity_id(entity.domain, DOMAIN, unique_id)
        if existing_id is not None and existing_id != entity.entity_id:
            # Keep the original real-MAC entity and its user customizations.
            _LOGGER.warning(
                "Removing duplicate Marstek entity %s; use %s in dashboards and automations",
                entity.entity_id,
                existing_id,
            )
            entities.async_remove(entity.entity_id)
        elif entity.unique_id != unique_id or entity.device_id != target.id:
            entities.async_update_entity(
                entity.entity_id, new_unique_id=unique_id, device_id=target.id
            )

    for device in [*matching_devices, *placeholder_devices]:
        if device.id == target.id:
            continue
        remaining = er.async_entries_for_device(
            entities, device.id, include_disabled_entities=True
        )
        if any(entity.config_entry_id == entry.entry_id for entity in remaining):
            continue
        if _exclusively_owned(device, entry):
            if remaining:
                continue
            _LOGGER.warning(
                "Removing duplicate Marstek device %s; retained device is %s",
                device.id,
                target.id,
            )
            # async_remove was renamed in HA 2026.9.
            remove = getattr(devices, "async_remove_device", None)
            if remove is None:
                remove = devices.async_remove
            remove(device.id)
        else:
            # A legacy shared placeholder may still belong to another battery.
            devices.async_update_device(
                device.id, remove_config_entry_id=entry.entry_id
            )

    return target.id
