"""Tests for migration of duplicate devices and entities left by older releases."""

import pytest
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.marstek.const import DOMAIN
from custom_components.marstek.registry import async_repair_registry

pytestmark = pytest.mark.asyncio


def repair(hass, entry):
    """Run registry migration with a validated identity."""
    return async_repair_registry(
        hass,
        entry,
        entry.unique_id,
        dr.DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
            name="Venus A Battery System",
            manufacturer="Marstek",
            model="Venus A",
            sw_version="123",
        ),
    )


def add_entity(registry, entry, device, unique_id):
    """Register an entity exactly as the old integration did."""
    return registry.async_get_or_create(
        "sensor",
        DOMAIN,
        unique_id,
        config_entry=entry,
        device_id=device.id,
    )


@pytest.mark.parametrize("placeholder", ["unknown", "", "None"])
async def test_promote_placeholder_preserves_ids_and_customizations(
    hass,
    marstek_entry,
    device_registry,
    entity_registry,
    placeholder,
):
    """A lone placeholder is corrected in place, retaining user references."""
    marstek_entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=marstek_entry.entry_id,
        identifiers={(DOMAIN, placeholder)},
        name="Marstek Battery System",
    )
    device_registry.async_update_device(device.id, name_by_user="Garage battery")
    entity = add_entity(
        entity_registry, marstek_entry, device, f"{placeholder}_battery_soc"
    )
    entity_registry.async_update_entity(
        entity.entity_id, name="My SOC", disabled_by=er.RegistryEntryDisabler.USER
    )

    assert repair(hass, marstek_entry) == device.id
    updated = entity_registry.async_get(entity.entity_id)
    assert updated.unique_id == f"{marstek_entry.unique_id}_battery_soc"
    assert updated.device_id == device.id
    assert updated.name == "My SOC"
    assert updated.disabled_by is er.RegistryEntryDisabler.USER
    assert device_registry.async_get(device.id).name_by_user == "Garage battery"
    assert device_registry.async_get(device.id).identifiers == {
        (DOMAIN, marstek_entry.unique_id)
    }
    assert len(device_registry.devices) == 1
    assert repair(hass, marstek_entry) == device.id
    assert len(entity_registry.entities) == 1


@pytest.mark.parametrize("real_mac", ["AA:BB:CC:DD:EE:01", "aabbccddee01"])
async def test_duplicate_cleanup_keeps_real_device_and_original_entity(
    hass,
    marstek_entry,
    device_registry,
    entity_registry,
    caplog,
    real_mac,
):
    """When both identities exist, preserve real-MAC records and move unique entities."""
    marstek_entry.add_to_hass(hass)
    real = device_registry.async_get_or_create(
        config_entry_id=marstek_entry.entry_id,
        identifiers={(DOMAIN, real_mac)},
        name="Venus A Battery System",
    )
    placeholder = device_registry.async_get_or_create(
        config_entry_id=marstek_entry.entry_id,
        identifiers={(DOMAIN, "unknown")},
        name="Marstek Battery System",
    )
    duplicate = add_entity(
        entity_registry, marstek_entry, placeholder, "unknown_battery_soc"
    )
    original = add_entity(
        entity_registry, marstek_entry, real, f"{real_mac}_battery_soc"
    )
    extra = add_entity(
        entity_registry, marstek_entry, placeholder, "unknown_battery_temperature"
    )
    entity_registry.async_update_entity(original.entity_id, name="Original SOC")

    assert repair(hass, marstek_entry) == real.id
    await hass.async_block_till_done()
    assert entity_registry.async_get(original.entity_id).name == "Original SOC"
    assert entity_registry.async_get(duplicate.entity_id) is None
    assert entity_registry.async_get(extra.entity_id).device_id == real.id
    assert device_registry.async_get(placeholder.id) is None
    assert duplicate.entity_id in caplog.text
    assert original.entity_id in caplog.text
    assert repair(hass, marstek_entry) == real.id
    assert len(entity_registry.entities) == 2


async def test_mac_format_migration_preserves_registry_ids(
    hass,
    marstek_entry,
    device_registry,
    entity_registry,
):
    """A different stored MAC spelling is corrected without recreating entities."""
    marstek_entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=marstek_entry.entry_id,
        identifiers={(DOMAIN, "aabbccddee01")},
    )
    entity = add_entity(
        entity_registry, marstek_entry, device, "aabbccddee01_battery_soc"
    )
    assert repair(hass, marstek_entry) == device.id
    assert (
        entity_registry.async_get(entity.entity_id).unique_id
        == f"{marstek_entry.unique_id}_battery_soc"
    )


async def test_migration_does_not_touch_other_entry(
    hass,
    marstek_entry,
    device_registry,
    entity_registry,
):
    """An unknown entity owned by another battery cannot be claimed or deleted."""
    marstek_entry.add_to_hass(hass)
    second = MockConfigEntry(domain=DOMAIN, unique_id="AA:BB:CC:DD:EE:02", data={})
    second.add_to_hass(hass)
    other_device = device_registry.async_get_or_create(
        config_entry_id=second.entry_id,
        identifiers={(DOMAIN, "unknown")},
    )
    other_entity = add_entity(
        entity_registry, second, other_device, "unknown_battery_soc"
    )
    repair(hass, marstek_entry)
    assert device_registry.async_get(other_device.id) == other_device
    assert entity_registry.async_get(other_entity.entity_id) == other_entity
    assert len(device_registry.devices) == 2


async def test_shared_legacy_placeholder_splits_by_entity_ownership(
    hass,
    marstek_entry,
    device_registry,
    entity_registry,
):
    """Older HA can share the placeholder device across two config entries."""
    marstek_entry.add_to_hass(hass)
    second = MockConfigEntry(domain=DOMAIN, unique_id="AA:BB:CC:DD:EE:02", data={})
    second.add_to_hass(hass)
    shared = device_registry.async_get_or_create(
        config_entry_id=marstek_entry.entry_id,
        identifiers={(DOMAIN, "unknown")},
    )
    if hasattr(shared, "config_entry_id"):
        pytest.skip("This HA version scopes device identities per config entry")
    device_registry.async_get_or_create(
        config_entry_id=second.entry_id,
        identifiers={(DOMAIN, "unknown")},
    )
    first_entity = add_entity(
        entity_registry, marstek_entry, shared, "unknown_battery_soc"
    )
    second_entity = add_entity(
        entity_registry, second, shared, "unknown_battery_temperature"
    )

    first_device_id = repair(hass, marstek_entry)
    assert first_device_id != shared.id
    assert (
        entity_registry.async_get(first_entity.entity_id).device_id == first_device_id
    )
    assert entity_registry.async_get(second_entity.entity_id).device_id == shared.id
    second_device_id = repair(hass, second)
    assert second_device_id != first_device_id
    assert (
        entity_registry.async_get(second_entity.entity_id).device_id == second_device_id
    )
    assert len(device_registry.devices) == 2


async def test_conflicting_entity_owned_by_other_entry_is_not_removed(
    hass,
    marstek_entry,
    device_registry,
    entity_registry,
):
    """Ambiguous legacy ownership must not silently steal a registered entity."""
    marstek_entry.add_to_hass(hass)
    second = MockConfigEntry(domain=DOMAIN, unique_id="AA:BB:CC:DD:EE:02", data={})
    second.add_to_hass(hass)
    placeholder = device_registry.async_get_or_create(
        config_entry_id=marstek_entry.entry_id,
        identifiers={(DOMAIN, "unknown")},
    )
    real = device_registry.async_get_or_create(
        config_entry_id=second.entry_id,
        identifiers={(DOMAIN, second.unique_id)},
    )
    ambiguous = add_entity(
        entity_registry, marstek_entry, placeholder, "unknown_battery_soc"
    )
    conflict = add_entity(
        entity_registry, second, real, f"{marstek_entry.unique_id}_battery_soc"
    )
    with pytest.raises(ConfigEntryError, match="belongs to another config entry"):
        repair(hass, marstek_entry)
    assert (
        entity_registry.async_get(ambiguous.entity_id).unique_id
        == "unknown_battery_soc"
    )
    assert entity_registry.async_get(conflict.entity_id) == conflict
    assert device_registry.async_get(placeholder.id) == placeholder
