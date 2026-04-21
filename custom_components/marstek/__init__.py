"""The Marstek Battery System integration."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .marstek_api import MarstekAPI
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.NUMBER,
]

SCAN_INTERVAL = timedelta(seconds=30)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Marstek Battery System from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    api = MarstekAPI(
        host=entry.data["host"],
        port=entry.data.get("port", 30000),
    )

    coordinator = MarstekDataUpdateCoordinator(hass, api, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await async_register_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


class MarstekDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Marstek data."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: MarstekAPI,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        self.api = api
        self.entry = entry
        self.device_info = None

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )

    async def _async_update_data(self):
        """Update data via library."""
        try:
            data = {}

            if self.device_info is None:
                self.device_info = await self.hass.async_add_executor_job(
                    self.api.get_device_info
                )

            wifi_status = await self.hass.async_add_executor_job(self.api.get_wifi_status)
            if wifi_status:
                data["wifi"] = wifi_status

            ble_status = await self.hass.async_add_executor_job(self.api.get_ble_status)
            if ble_status:
                data["ble"] = ble_status

            bat_status = await self.hass.async_add_executor_job(self.api.get_battery_status)
            if bat_status:
                data["battery"] = bat_status

            pv_status = await self.hass.async_add_executor_job(self.api.get_pv_status)
            if pv_status:
                data["pv"] = pv_status

            es_status = await self.hass.async_add_executor_job(self.api.get_es_status)
            if es_status:
                data["es"] = es_status

            es_mode = await self.hass.async_add_executor_job(self.api.get_es_mode)
            if es_mode:
                data["es_mode"] = es_mode

            em_status = await self.hass.async_add_executor_job(self.api.get_em_status)
            if em_status:
                data["em"] = em_status

            data["device_info"] = self.device_info
            return data

        except Exception as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err