"""Tests for Marstek services."""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.marstek.const import DOMAIN
from custom_components.marstek.services import (
    SERVICE_SET_OPERATING_MODE_PASSIVE,
    async_register_services,
)


def _add_config_entry(
    hass: HomeAssistant,
    entry_id: str,
    *,
    domain: str = DOMAIN,
) -> MockConfigEntry:
    """Add a config entry to Home Assistant."""
    entry = MockConfigEntry(domain=domain, entry_id=entry_id, data={})
    entry.add_to_hass(hass)
    return entry


def _register_entity(
    entity_registry: er.EntityRegistry,
    config_entry: MockConfigEntry | None,
    entity_id: str,
    *,
    platform: str = DOMAIN,
    unique_id: str,
) -> None:
    """Register an entity with the requested entity ID."""
    entry = entity_registry.async_get_or_create(
        "select",
        platform,
        unique_id,
        config_entry=config_entry,
        suggested_object_id=f"generated_{unique_id}",
    )
    if entry.entity_id != entity_id:
        entity_registry.async_update_entity(
            entry.entity_id,
            new_entity_id=entity_id,
        )


async def _call_service(hass: HomeAssistant, entity_id: str, power: int = -500) -> None:
    """Call the passive-mode service."""
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_OPERATING_MODE_PASSIVE,
        {
            "entity_id": entity_id,
            "power": power,
            "cd_time": 86400,
        },
        blocking=True,
    )


@pytest.mark.asyncio
async def test_service_targets_only_device(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
) -> None:
    """A service call with one device targets its owning coordinator."""
    config_entry = _add_config_entry(hass, "entry_one")
    coordinator = AsyncMock()
    coordinator.async_set_passive_power.return_value = True
    hass.data[DOMAIN] = {config_entry.entry_id: coordinator}
    entity_id = "select.marstek_battery_system_operating_mode"
    _register_entity(
        entity_registry,
        config_entry,
        entity_id,
        unique_id="first_operating_mode",
    )
    await async_register_services(hass)

    await _call_service(hass, entity_id)

    coordinator.async_set_passive_power.assert_awaited_once_with(-500)


@pytest.mark.asyncio
async def test_service_targets_each_of_two_devices(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
) -> None:
    """Each entity resolves to its own coordinator with two config entries."""
    first_entry = _add_config_entry(hass, "entry_one")
    second_entry = _add_config_entry(hass, "entry_two")
    first_coordinator = AsyncMock()
    second_coordinator = AsyncMock()
    first_coordinator.async_set_passive_power.return_value = True
    second_coordinator.async_set_passive_power.return_value = True
    hass.data[DOMAIN] = {
        first_entry.entry_id: first_coordinator,
        second_entry.entry_id: second_coordinator,
    }
    first_entity_id = "select.first_battery_operating_mode"
    second_entity_id = "select.second_battery_operating_mode"
    _register_entity(
        entity_registry,
        first_entry,
        first_entity_id,
        unique_id="first_operating_mode",
    )
    _register_entity(
        entity_registry,
        second_entry,
        second_entity_id,
        unique_id="second_operating_mode",
    )
    await async_register_services(hass)

    await _call_service(hass, first_entity_id, 400)

    first_coordinator.async_set_passive_power.assert_awaited_once_with(400)
    second_coordinator.async_set_passive_power.assert_not_awaited()

    first_coordinator.reset_mock()
    await _call_service(hass, second_entity_id, -700)

    first_coordinator.async_set_passive_power.assert_not_awaited()
    second_coordinator.async_set_passive_power.assert_awaited_once_with(-700)


@pytest.mark.parametrize(
    "entity_id",
    (
        "select.user_renamed_battery_mode",
        "select.marstek_battery_system_operating_mode_2",
    ),
)
@pytest.mark.asyncio
async def test_service_targets_renamed_or_suffixed_entity(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    entity_id: str,
) -> None:
    """Entity text does not affect coordinator resolution."""
    config_entry = _add_config_entry(hass, "entry_one")
    coordinator = AsyncMock()
    coordinator.async_set_passive_power.return_value = True
    hass.data[DOMAIN] = {config_entry.entry_id: coordinator}
    _register_entity(
        entity_registry,
        config_entry,
        entity_id,
        unique_id="operating_mode",
    )
    await async_register_services(hass)

    await _call_service(hass, entity_id)

    coordinator.async_set_passive_power.assert_awaited_once_with(-500)


@pytest.mark.asyncio
async def test_service_rejects_unknown_entity(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An entity absent from the registry does not target a coordinator."""
    coordinator = AsyncMock()
    hass.data[DOMAIN] = {"entry_one": coordinator}
    await async_register_services(hass)

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(ServiceValidationError),
    ):
        await _call_service(hass, "select.unknown_marstek_entity")

    coordinator.async_set_passive_power.assert_not_awaited()
    assert (
        "Could not resolve Marstek device for "
        "entity_id=select.unknown_marstek_entity" in caplog.text
    )


@pytest.mark.asyncio
async def test_service_rejects_entity_from_another_integration(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An entity from another integration cannot select a Marstek coordinator."""
    config_entry = _add_config_entry(
        hass,
        "entry_one",
        domain="other_integration",
    )
    coordinator = AsyncMock()
    hass.data[DOMAIN] = {config_entry.entry_id: coordinator}
    entity_id = "select.other_integration_operating_mode"
    _register_entity(
        entity_registry,
        config_entry,
        entity_id,
        platform="other_integration",
        unique_id="other_operating_mode",
    )
    await async_register_services(hass)

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(ServiceValidationError),
    ):
        await _call_service(hass, entity_id)

    coordinator.async_set_passive_power.assert_not_awaited()
    assert f"Could not resolve Marstek device for entity_id={entity_id}" in caplog.text


@pytest.mark.parametrize("has_config_entry", (False, True))
@pytest.mark.asyncio
async def test_service_rejects_missing_config_entry_or_coordinator(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    caplog: pytest.LogCaptureFixture,
    has_config_entry: bool,
) -> None:
    """An unassociated entity or unloaded config entry cannot be targeted."""
    config_entry = (
        _add_config_entry(hass, "entry_without_coordinator")
        if has_config_entry
        else None
    )
    hass.data[DOMAIN] = {}
    entity_id = "select.orphaned_marstek_operating_mode"
    _register_entity(
        entity_registry,
        config_entry,
        entity_id,
        unique_id="orphaned_operating_mode",
    )
    await async_register_services(hass)

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(ServiceValidationError),
    ):
        await _call_service(hass, entity_id)

    assert f"Could not resolve Marstek device for entity_id={entity_id}" in caplog.text


@pytest.mark.asyncio
async def test_service_raises_when_the_coordinator_command_fails(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
) -> None:
    """A device-rejected Passive command must fail the service call, not just log."""
    config_entry = _add_config_entry(hass, "entry_one")
    coordinator = AsyncMock()
    coordinator.async_set_passive_power.return_value = False
    hass.data[DOMAIN] = {config_entry.entry_id: coordinator}
    entity_id = "select.marstek_battery_system_operating_mode"
    _register_entity(
        entity_registry,
        config_entry,
        entity_id,
        unique_id="first_operating_mode",
    )
    await async_register_services(hass)

    with pytest.raises(HomeAssistantError):
        await _call_service(hass, entity_id)

    coordinator.async_set_passive_power.assert_awaited_once_with(-500)
