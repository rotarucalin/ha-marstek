"""The Marstek Battery System integration."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    MODE_AI,
    MODE_AUTO,
    MODE_MANUAL,
    MODE_PASSIVE,
    PASSIVE_STATE_ACKNOWLEDGED,
    PASSIVE_STATE_RETRYING,
    PASSIVE_STATE_SENT,
    PASSIVE_STATE_UNKNOWN,
)
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
PASSIVE_POWER_KEEPALIVE_SECONDS = 180
PASSIVE_POWER_TOLERANCE = 0.20
PASSIVE_POWER_ZERO_TOLERANCE = 10


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
        await hass.data[DOMAIN][entry.entry_id].async_stop_passive_control()
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
        self._passive_power_target: int | None = None
        self._passive_control_generation = 0
        self._passive_keepalive_cancel: Callable[[], None] | None = None
        self._passive_command_lock = asyncio.Lock()
        self._passive_power_state = PASSIVE_STATE_UNKNOWN

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )

    @property
    def passive_power_state(self) -> str:
        """Return the current passive power control state."""
        return self._passive_power_state

    def _set_passive_power_state(self, state: str) -> None:
        """Update the passive power state and notify listeners immediately."""
        if state == self._passive_power_state:
            return
        self._passive_power_state = state
        self.async_update_listeners()

    async def async_set_passive_power(self, power: int) -> bool:
        """Set and maintain a passive mode power target."""
        async with self._passive_command_lock:
            self._passive_power_target = power
            self._passive_control_generation += 1
            self._cancel_passive_keepalive()
            return await self._async_send_passive_power(PASSIVE_STATE_SENT)

    async def async_set_operating_mode(self, mode: str) -> bool:
        """Set an operating mode, superseding any passive power target."""
        async with self._passive_command_lock:
            if mode == MODE_PASSIVE:
                return True

            self._clear_passive_control()

            if mode == MODE_AUTO:
                return await self.hass.async_add_executor_job(self.api.set_es_mode_auto)
            if mode == MODE_AI:
                return await self.hass.async_add_executor_job(self.api.set_es_mode_ai)
            if mode == MODE_MANUAL:
                return await self.hass.async_add_executor_job(
                    self.api.set_es_mode_manual, 0, "00:00", "23:59", 127, 100, 1
                )

            return False

    async def async_stop_passive_control(self) -> None:
        """Stop maintaining passive mode power."""
        async with self._passive_command_lock:
            self._clear_passive_control()

    async def _async_send_passive_power(self, outcome_state: str) -> bool:
        """Send the current passive power target and schedule its keepalive.

        Called both by the 180s keepalive and by poll-driven verify retries;
        either path reschedules the same keepalive timer, so once the target
        is confirmed the keepalive alone keeps it alive every 180s.
        """
        if self._passive_power_target is None:
            return False

        power = self._passive_power_target
        success = await self.hass.async_add_executor_job(
            self.api.set_es_mode_passive,
            power,
        )
        self._schedule_passive_keepalive()
        if success:
            self._set_passive_power_state(outcome_state)
        return success

    def _schedule_passive_keepalive(self) -> None:
        """Schedule a command-only passive power keepalive."""
        self._cancel_passive_keepalive()
        generation = self._passive_control_generation
        self._passive_keepalive_cancel = async_call_later(
            self.hass,
            PASSIVE_POWER_KEEPALIVE_SECONDS,
            lambda _now: self.hass.async_create_task(
                self._async_keepalive_passive_power(generation)
            ),
        )

    def _cancel_passive_keepalive(self) -> None:
        """Cancel the outstanding passive power keepalive, if any."""
        if self._passive_keepalive_cancel is not None:
            self._passive_keepalive_cancel()
            self._passive_keepalive_cancel = None

    def _clear_passive_control(self) -> None:
        """Discard the passive power target and invalidate queued callbacks."""
        self._passive_power_target = None
        self._passive_control_generation += 1
        self._cancel_passive_keepalive()
        self._set_passive_power_state(PASSIVE_STATE_UNKNOWN)

    async def _async_keepalive_passive_power(self, generation: int) -> None:
        """Resend the passive power target when its keepalive is due."""
        async with self._passive_command_lock:
            if (
                generation != self._passive_control_generation
                or self._passive_power_target is None
            ):
                return

            self._passive_keepalive_cancel = None
            await self._async_send_passive_power(PASSIVE_STATE_SENT)

    def _passive_power_is_confirmed(self, mode_data: dict) -> bool:
        """Return whether fresh mode data confirms the passive power target."""
        if self._passive_power_target is None or mode_data.get("mode") != MODE_PASSIVE:
            return False

        reported_power = mode_data.get("ongrid_power")
        if not isinstance(reported_power, (int, float)):
            return False

        tolerance = max(
            abs(self._passive_power_target) * PASSIVE_POWER_TOLERANCE,
            PASSIVE_POWER_ZERO_TOLERANCE,
        )
        return abs(reported_power - self._passive_power_target) <= tolerance

    async def _async_verify_passive_power(self, mode_data: dict) -> None:
        """Retry the target when fresh mode data does not confirm it."""
        async with self._passive_command_lock:
            if self._passive_power_target is None:
                return

            if self._passive_power_is_confirmed(mode_data):
                self._set_passive_power_state(PASSIVE_STATE_ACKNOWLEDGED)
                return

            _LOGGER.debug(
                "Passive power target %s W not confirmed by reported mode data; retrying",
                self._passive_power_target,
            )
            await self._async_send_passive_power(PASSIVE_STATE_RETRYING)

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
                if self._missing_cycles.get("es_mode") == 0:
                    await self._async_verify_passive_power(es_mode)

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