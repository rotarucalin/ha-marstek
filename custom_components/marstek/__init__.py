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
        self._last_good_data: dict = {}
        self._missing_cycles: dict[str, int] = {}

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )

    async def _fetch_section(self, key: str, fetcher):
        """Fetch one section and track consecutive misses."""
        result = await self.hass.async_add_executor_job(fetcher)

        if result is not None:
            self._missing_cycles[key] = 0
            self._last_good_data[key] = result
            return result

        self._missing_cycles[key] = self._missing_cycles.get(key, 0) + 1
        _LOGGER.debug(
            "Marstek section %s unavailable this cycle (miss %s)",
            key,
            self._missing_cycles[key],
        )

        # Keep the last good value for up to 6 missed cycles (3 minutes) before giving up and returning None
        if self._missing_cycles[key] <= 6 and key in self._last_good_data:
            _LOGGER.debug("Using cached Marstek section %s", key)
            return self._last_good_data[key]

        return None

    async def _async_update_data(self):
        """Update data via library."""
        try:
            data = {}

            if self.device_info is None:
                self.device_info = await self.hass.async_add_executor_job(
                    self.api.get_device_info
                )

            if self.device_info is not None:
                data["device_info"] = self.device_info

            wifi_status = await self._fetch_section("wifi", self.api.get_wifi_status)
            if wifi_status is not None:
                data["wifi"] = wifi_status

            ble_status = await self._fetch_section("ble", self.api.get_ble_status)
            if ble_status is not None:
                data["ble"] = ble_status

            bat_status = await self._fetch_section("battery", self.api.get_battery_status)
            if bat_status is not None:
                data["battery"] = bat_status

            pv_status = await self._fetch_section("pv", self.api.get_pv_status)
            if pv_status is not None:
                data["pv"] = pv_status

            es_status = await self._fetch_section("es", self.api.get_es_status)
            if es_status is not None:
                data["es"] = es_status

            es_mode = await self._fetch_section("es_mode", self.api.get_es_mode)
            if es_mode is not None:
                data["es_mode"] = es_mode

            em_status = await self._fetch_section("em", self.api.get_em_status)
            if em_status is not None:
                data["em"] = em_status

            # If absolutely nothing useful came back, treat this as a real update failure
            non_device_keys = [k for k in data if k != "device_info"]
            if not non_device_keys:
                raise UpdateFailed("No Marstek data received from any endpoint")

            return data

        except UpdateFailed:
            raise
        except Exception as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err