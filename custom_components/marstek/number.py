"""Support for Marstek Battery System number entities."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MarstekDataUpdateCoordinator
from .const import DOMAIN, MODE_PASSIVE
from .entity import MarstekEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Marstek number entities based on a config entry."""
    coordinator: MarstekDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MarstekPassivePowerNumber(coordinator)])


class MarstekPassivePowerNumber(MarstekEntity, NumberEntity):
    """Representation of Marstek passive mode power setting."""

    _attr_mode = NumberMode.BOX
    _attr_native_step = 10
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator: MarstekDataUpdateCoordinator) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator, "passive_power")
        self._attr_name = "Passive Mode Power"

    @property
    def native_min_value(self) -> float:
        """Return the configured lower bound for this device."""
        return self.coordinator.calibration.command_min

    @property
    def native_max_value(self) -> float:
        """Return the configured upper bound for this device."""
        return self.coordinator.calibration.command_max

    @property
    def native_value(self) -> float | None:
        """Return the maintained passive power target."""
        if "es_mode" not in self.coordinator.data:
            return None

        mode_data = self.coordinator.data["es_mode"]
        if mode_data is None or mode_data.get("mode") != MODE_PASSIVE:
            return None

        if self.coordinator.desired_power is not None:
            return self.coordinator.desired_power

        return mode_data.get("ongrid_power")

    async def async_set_native_value(self, value: float) -> None:
        """Set new value."""
        await self.coordinator.async_set_passive_power(int(value))

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        if not super().available or "es_mode" not in self.coordinator.data:
            return False

        mode_data = self.coordinator.data["es_mode"]
        if mode_data is None:
            return False

        return mode_data.get("mode") == MODE_PASSIVE
