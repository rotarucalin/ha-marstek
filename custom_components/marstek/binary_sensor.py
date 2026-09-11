"""Support for Marstek Battery System binary sensors."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MarstekDataUpdateCoordinator
from .const import DOMAIN
from .entity import MarstekEntity

_LOGGER = logging.getLogger(__name__)


@dataclass
class MarstekBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes Marstek binary sensor entity."""

    value_fn: Callable[[dict], bool | None] | None = None
    data_key: str | None = None


BINARY_SENSOR_TYPES: tuple[MarstekBinarySensorEntityDescription, ...] = (
    MarstekBinarySensorEntityDescription(
        key="battery_charging",
        name="Battery Charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        data_key="battery",
        value_fn=lambda data: data.get("charg_flag"),
    ),
    MarstekBinarySensorEntityDescription(
        key="battery_discharging",
        name="Battery Discharging",
        data_key="battery",
        value_fn=lambda data: data.get("dischrg_flag"),
    ),
    MarstekBinarySensorEntityDescription(
        key="bluetooth_connected",
        name="Bluetooth Connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        data_key="ble",
        value_fn=lambda data: data.get("state") == "connect",
    ),
    MarstekBinarySensorEntityDescription(
        key="ct_connected",
        name="CT Connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        data_key="em",
        value_fn=lambda data: data.get("ct_state") == 1,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Marstek binary sensors based on a config entry."""
    coordinator: MarstekDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    for description in BINARY_SENSOR_TYPES:
        entities.append(MarstekBinarySensor(coordinator, description))

    async_add_entities(entities)


class MarstekBinarySensor(MarstekEntity, BinarySensorEntity):
    """Representation of a Marstek binary sensor."""

    entity_description: MarstekBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: MarstekDataUpdateCoordinator,
        description: MarstekBinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Return whether the entity is available."""
        key = self.entity_description.data_key
        return (
            super().available
            and key is not None
            and key in self.coordinator.data
            and self.coordinator.data[key] is not None
        )

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        if not self.available:
            return None

        data = self.coordinator.data[self.entity_description.data_key]

        if self.entity_description.value_fn:
            return self.entity_description.value_fn(data)

        return None
