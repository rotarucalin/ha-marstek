"""Support for Marstek Battery System select entities."""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MarstekDataUpdateCoordinator
from .const import DOMAIN, MODE_AI, MODE_AUTO, MODE_MANUAL, MODE_PASSIVE, OPERATING_MODES

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Marstek select entities based on a config entry."""
    coordinator: MarstekDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MarstekOperatingModeSelect(coordinator)])


class MarstekOperatingModeSelect(CoordinatorEntity, SelectEntity):
    """Representation of Marstek operating mode select."""

    def __init__(self, coordinator: MarstekDataUpdateCoordinator) -> None:
        """Initialize the select entity."""
        super().__init__(coordinator)

        device_info = coordinator.data.get("device_info", {})
        device_name = device_info.get("device", "Marstek")
        ble_mac = device_info.get("ble_mac", "unknown")

        self._attr_unique_id = f"{ble_mac}_operating_mode"
        self._attr_name = "Operating Mode"
        self._attr_options = OPERATING_MODES
        self._attr_device_info = {
            "identifiers": {(DOMAIN, ble_mac)},
            "name": f"{device_name} Battery System",
            "manufacturer": "Marstek",
            "model": device_info.get("device", "Unknown"),
            "sw_version": str(device_info.get("ver", "")),
        }

    @property
    def current_option(self) -> str | None:
        """Return the selected entity option to represent the entity state."""
        if "es_mode" not in self.coordinator.data:
            return None

        mode_data = self.coordinator.data["es_mode"]
        if mode_data is None:
            return None

        return mode_data.get("mode")

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        success = await self.coordinator.async_set_operating_mode(option)

        if success:
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.error("Failed to set operating mode to %s", option)