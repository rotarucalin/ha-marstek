"""Support for Marstek Battery System number entities."""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MarstekDataUpdateCoordinator
from .const import DOMAIN, MODE_PASSIVE

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Marstek number entities based on a config entry."""
    coordinator: MarstekDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MarstekPassivePowerNumber(coordinator)])


class MarstekPassivePowerNumber(CoordinatorEntity, NumberEntity):
    """Representation of Marstek passive mode power setting."""

    _attr_mode = NumberMode.BOX
    _attr_native_min_value = -3000
    _attr_native_max_value = 3000
    _attr_native_step = 10
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator: MarstekDataUpdateCoordinator) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)

        device_info = coordinator.data.get("device_info", {})
        device_name = device_info.get("device", "Marstek")
        ble_mac = device_info.get("ble_mac", "unknown")

        self._attr_unique_id = f"{ble_mac}_passive_power"
        self._attr_name = "Passive Mode Power"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, ble_mac)},
            "name": f"{device_name} Battery System",
            "manufacturer": "Marstek",
            "model": device_info.get("device", "Unknown"),
            "sw_version": str(device_info.get("ver", "")),
        }

    @property
    def native_value(self) -> float | None:
        """Return the current value."""
        if "es_mode" not in self.coordinator.data:
            return None

        mode_data = self.coordinator.data["es_mode"]
        if mode_data is None or mode_data.get("mode") != MODE_PASSIVE:
            return None

        return mode_data.get("ongrid_power")

    async def async_set_native_value(self, value: float) -> None:
        """Set new value."""
        success = await self.coordinator.async_set_passive_power(int(value))

        if not success:
            _LOGGER.error("Failed to set passive mode power to %s W", value)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        if "es_mode" not in self.coordinator.data:
            return False

        mode_data = self.coordinator.data["es_mode"]
        if mode_data is None:
            return False

        return mode_data.get("mode") == MODE_PASSIVE