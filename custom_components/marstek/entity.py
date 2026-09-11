"""Common identity for all entities belonging to one Marstek battery."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

if TYPE_CHECKING:
    from . import MarstekDataUpdateCoordinator


class MarstekEntity(CoordinatorEntity):
    """An entity whose identity is independent of API availability."""

    def __init__(self, coordinator: MarstekDataUpdateCoordinator, key: str) -> None:
        """Initialize with the config entry's persistent hardware identity."""
        super().__init__(coordinator)
        if coordinator.device_id is None:
            raise ValueError("Cannot create Marstek entities without a device identity")
        self._attr_unique_id = f"{coordinator.device_id}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return shared device metadata, including information recovered later."""
        return self.coordinator.registry_device_info
