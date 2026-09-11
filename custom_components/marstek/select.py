"""Support for Marstek Battery System select entities."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MarstekDataUpdateCoordinator
from .const import DOMAIN, OPERATING_MODES
from .entity import MarstekEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Marstek select entities based on a config entry."""
    coordinator: MarstekDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MarstekOperatingModeSelect(coordinator)])


class MarstekOperatingModeSelect(MarstekEntity, SelectEntity):
    """Representation of Marstek operating mode select."""

    def __init__(self, coordinator: MarstekDataUpdateCoordinator) -> None:
        """Initialize the select entity."""
        super().__init__(coordinator, "operating_mode")
        self._attr_name = "Operating Mode"
        self._attr_options = OPERATING_MODES

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
