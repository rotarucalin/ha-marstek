"""The Marstek Battery System integration."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CALIBRATION_STORAGE_KEY_FMT,
    CALIBRATION_STORAGE_VERSION,
    CONF_MAX_PASSIVE_POWER,
    DEFAULT_MAX_PASSIVE_POWER,
    DOMAIN,
    MODE_AI,
    MODE_AUTO,
    MODE_MANUAL,
    MODE_PASSIVE,
    PASSIVE_DEADBAND_W,
    PASSIVE_MIN_LEARN_POWER_W,
    PASSIVE_RESEND_THRESHOLD_W,
    PASSIVE_SAVE_DELAY_SECONDS,
    PASSIVE_SETTLE_SECONDS,
    PASSIVE_SOC_LEARN_MAX,
    PASSIVE_SOC_LEARN_MIN,
    PASSIVE_STABILITY_SAMPLES,
    PASSIVE_STABILITY_TOLERANCE_W,
    PASSIVE_STATE_ACKNOWLEDGED,
    PASSIVE_STATE_RETRYING,
    PASSIVE_STATE_SENT,
    PASSIVE_STATE_UNKNOWN,
    SOURCE_CALIBRATION,
    SOURCE_COMPENSATION,
    SOURCE_DIRECT,
    SOURCE_SATURATED,
)
from .identity import CONF_DEVICE_INFO, device_metadata, normalize_mac
from .marstek_api import MarstekAPI
from .passive_calibration import PassiveCalibration, bucket_center, direction_of
from .registry import async_repair_registry
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)

# Seam so tests can drive the settle and stability windows deterministically.
monotonic = time.monotonic

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.NUMBER,
]

SCAN_INTERVAL = timedelta(seconds=30)
WIFI_POLL_INTERVAL_SECONDS = 300
# EM is disabled only on an explicit CT disconnection, not on request failure.
OPTIONAL_SECTIONS = frozenset({"ble", "pv"})
PASSIVE_POWER_KEEPALIVE_SECONDS = 180
PASSIVE_POWER_RETRY_SECONDS = 15
PASSIVE_POWER_TOLERANCE = 0.20
PASSIVE_POWER_ZERO_TOLERANCE = 10


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Marstek Battery System from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    api = MarstekAPI(
        host=entry.data["host"],
        port=entry.data.get("port", 30000),
    )

    store = Store(
        hass,
        CALIBRATION_STORAGE_VERSION,
        CALIBRATION_STORAGE_KEY_FMT.format(entry_id=entry.entry_id),
    )
    max_passive_power = entry.options.get(
        CONF_MAX_PASSIVE_POWER,
        entry.data.get(CONF_MAX_PASSIVE_POWER, DEFAULT_MAX_PASSIVE_POWER),
    )
    coordinator = MarstekDataUpdateCoordinator(
        hass,
        api,
        entry,
        store=store,
        calibration_data=await store.async_load(),
        max_passive_power=max_passive_power,
    )
    await coordinator.async_config_entry_first_refresh()

    coordinator.registry_device_id = async_repair_registry(
        hass, entry, coordinator.device_id, coordinator.registry_device_info
    )

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await async_register_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # A changed "max passive power" option needs a fresh calibration clamp.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry after its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator = hass.data[DOMAIN][entry.entry_id]
        await coordinator.async_stop_passive_control()
        # Flush anything the debounced save has not written yet.
        await coordinator.async_save_calibration()
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


class MarstekDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Marstek data."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: MarstekAPI,
        entry: ConfigEntry,
        *,
        store: Store | None = None,
        calibration_data: object = None,
        max_passive_power: int = DEFAULT_MAX_PASSIVE_POWER,
    ) -> None:
        """Initialize."""
        self.api = api
        self.entry = entry
        # Retain the exact stored spelling so existing entity IDs remain stable.
        self.device_id = entry.unique_id if normalize_mac(entry.unique_id) else None
        cached_info = entry.data.get(CONF_DEVICE_INFO, {})
        self.device_info = (
            device_metadata(cached_info) if isinstance(cached_info, dict) else {}
        )
        self.registry_device_id: str | None = None
        self._device_info_fetched = False
        self._identity_mismatch = False
        if self.device_id is not None:
            self.device_info["ble_mac"] = self.device_id
            # Older entries stored only host/port. Recover their display metadata
            # from the real device if GetDevice is unavailable at startup.
            for device in dr.async_entries_for_config_entry(
                dr.async_get(hass), entry.entry_id
            ):
                if any(
                    domain == DOMAIN
                    and normalize_mac(identifier) == normalize_mac(self.device_id)
                    for domain, identifier in device.identifiers
                ):
                    if device.model and device.model != "Unknown":
                        self.device_info.setdefault("device", device.model)
                    if device.sw_version:
                        self.device_info.setdefault("ver", device.sw_version)
        self._last_good_data: dict = {}
        self._missing_cycles: dict[str, int] = {}
        self._next_wifi_poll_at = 0.0
        # Stop probing failed PV/Bluetooth or an explicitly disconnected CT.
        # Probe again when a fresh coordinator is created on startup/reload.
        self._disabled_optional_sections: set[str] = set()
        # The requested real output, the value actually sent, and the samples
        # measured since that value last changed.
        self._passive_desired_power: int | None = None
        self._passive_command_power: int | None = None
        self._passive_command_source = SOURCE_DIRECT
        self._passive_command_changed_at = 0.0
        self._passive_last_send_ok = False
        self._passive_samples: deque[tuple[float, float]] = deque(
            maxlen=PASSIVE_STABILITY_SAMPLES
        )
        self._passive_saturated_buckets: set[tuple[str, int]] = set()
        self._passive_control_generation = 0
        self._passive_keepalive_cancel: Callable[[], None] | None = None
        self._passive_command_lock = asyncio.Lock()
        self._passive_power_state = PASSIVE_STATE_UNKNOWN
        self._calibration_store = store
        self.calibration = PassiveCalibration.from_dict(
            calibration_data,
            command_min=-max_passive_power,
            command_max=max_passive_power,
        )

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=SCAN_INTERVAL,
        )

    @property
    def registry_device_info(self) -> dr.DeviceInfo:
        """Describe this battery without deriving its identity from polling data."""
        if self.device_id is None:
            raise ValueError("Marstek device identity has not been established")
        info = dr.DeviceInfo(
            identifiers={(DOMAIN, self.device_id)},
            name=f"{self.device_info.get('device', 'Marstek')} Battery System",
            manufacturer="Marstek",
        )
        if model := self.device_info.get("device"):
            info["model"] = str(model)
        if self.device_info.get("ver") is not None:
            info["sw_version"] = str(self.device_info["ver"])
        return info

    async def _async_refresh_device_info(self) -> None:
        """Refresh metadata, retrying misses without changing a saved identity."""
        if self._device_info_fetched:
            return
        info = await self.hass.async_add_executor_job(self.api.get_device_info)
        reported_id = (
            normalize_mac(info.get("ble_mac")) if isinstance(info, dict) else None
        )
        if reported_id is not None:
            if self.device_id is not None and reported_id != normalize_mac(
                self.device_id
            ):
                self._identity_mismatch = True
                await self.async_stop_passive_control()
                raise UpdateFailed(
                    "The configured address belongs to a different Marstek battery"
                )
            if self.device_id is None:
                if any(
                    other.entry_id != self.entry.entry_id
                    and normalize_mac(other.unique_id) == reported_id
                    for other in self.hass.config_entries.async_entries(DOMAIN)
                ):
                    raise UpdateFailed(
                        "This Marstek battery is already configured in another entry"
                    )
                self.device_id = reported_id
            self.device_info.update(device_metadata(info))
            self.device_info["ble_mac"] = self.device_id
            self._device_info_fetched = bool(self.device_info.get("device"))
            self._identity_mismatch = False
            if (
                self.entry.data.get(CONF_DEVICE_INFO) != self.device_info
                or self.entry.unique_id != self.device_id
            ):
                self.hass.config_entries.async_update_entry(
                    self.entry,
                    unique_id=self.device_id,
                    data={**self.entry.data, CONF_DEVICE_INFO: dict(self.device_info)},
                )
            if self.registry_device_id is not None:
                metadata = dict(self.registry_device_info)
                metadata.pop("identifiers")
                dr.async_get(self.hass).async_update_device(
                    self.registry_device_id, **metadata
                )
        if self._identity_mismatch:
            raise UpdateFailed(
                "Waiting for the configured Marstek battery identity to be verified"
            )
        if self.device_id is None:
            # first_refresh converts this to ConfigEntryNotReady. No platform
            # may create entities before a usable identity has been obtained.
            raise UpdateFailed(
                "No valid Marstek BLE MAC available; retrying device setup"
            )

    @property
    def passive_power_state(self) -> str:
        """Return the current passive power control state."""
        return self._passive_power_state

    @property
    def desired_power(self) -> int | None:
        """Return the real output requested through the service or number."""
        return self._passive_desired_power

    @property
    def command_power(self) -> int | None:
        """Return the compensated value actually sent to the device."""
        return self._passive_command_power

    def calibration_snapshot(self) -> dict:
        """Return the learned calibration in its persisted form."""
        return self.calibration.to_dict()

    async def async_save_calibration(self) -> None:
        """Persist the learned calibration immediately."""
        if self._calibration_store is not None:
            await self._calibration_store.async_save(self.calibration_snapshot())

    def _set_passive_power_state(self, state: str) -> None:
        """Update the passive power state and notify listeners immediately."""
        if state == self._passive_power_state:
            return
        self._passive_power_state = state
        self.async_update_listeners()

    def _set_passive_command_power(self, command: int, source: str) -> None:
        """Adopt a new outgoing value and restart its measurement window."""
        self._passive_command_power = command
        self._passive_command_source = source
        self._passive_command_changed_at = monotonic()
        self._passive_samples.clear()

    async def async_set_passive_power(self, power: int) -> bool:
        """Set and maintain a desired real output for passive mode."""
        async with self._passive_command_lock:
            if self._identity_mismatch:
                _LOGGER.warning(
                    "Marstek command blocked: device=%s source=new_target "
                    "mode=Passive power=%sW error=device identity mismatch",
                    self.entry.title,
                    power,
                )
                return False
            desired = max(
                self.calibration.command_min, min(self.calibration.command_max, power)
            )
            if desired != power:
                _LOGGER.warning(
                    "Marstek passive power request clamped: device=%s device_id=%s "
                    "requested=%sW limit=%sW..%sW",
                    self.entry.title,
                    self.device_id,
                    power,
                    self.calibration.command_min,
                    self.calibration.command_max,
                )
            self._passive_desired_power = desired
            command, source = self.calibration.command_for(desired)
            self._set_passive_command_power(command, source)
            self._log_passive_power(source)
            return await self._async_send_passive_power(
                PASSIVE_STATE_SENT, source="new_target"
            )

    async def async_set_operating_mode(
        self, mode: str, *, source: str = "operating_mode_select"
    ) -> bool:
        """Set an operating mode, superseding any passive power target."""
        async with self._passive_command_lock:
            if self._identity_mismatch:
                _LOGGER.warning(
                    "Marstek command blocked: device=%s source=%s mode=%s "
                    "error=device identity mismatch",
                    self.entry.title,
                    source,
                    mode,
                )
                return False
            if mode == MODE_PASSIVE:
                return True

            self._clear_passive_control()

            return await self._async_send_mode_command(
                mode=mode, power=100 if mode == MODE_MANUAL else None, source=source
            )

    async def async_stop_passive_control(self) -> None:
        """Stop maintaining passive mode power."""
        async with self._passive_command_lock:
            self._clear_passive_control()

    async def _async_send_mode_command(
        self, *, mode: str, source: str, power: int | None = None
    ) -> bool:
        """Log and send a mode command using the API's set_result semantics.

        A successful request still needs separate confirmation from ES.GetMode.
        Callers serialize commands with the passive command lock.
        """
        command: Callable[..., bool]
        args: tuple[int | str, ...]
        if mode == MODE_PASSIVE and power is not None:
            command, args = self.api.set_es_mode_passive, (power,)
        elif mode == MODE_AUTO:
            command, args = self.api.set_es_mode_auto, ()
        elif mode == MODE_AI:
            command, args = self.api.set_es_mode_ai, ()
        elif mode == MODE_MANUAL and power is not None:
            command, args = (
                self.api.set_es_mode_manual,
                (0, "00:00", "23:59", 127, power, 1),
            )
        else:
            return False

        context = (
            f"device={self.entry.title} device_id={self.device_id} "
            f"host={self.entry.data['host']} port={self.entry.data.get('port', 30000)} "
            f"source={source} method=ES.SetMode mode={mode}"
        )
        if power is not None:
            context += f" power={power}W"
        if mode == MODE_MANUAL:
            context += (
                " time_num=0 start_time=00:00 end_time=23:59 week_set=127 enable=1"
            )
        if source != "keepalive":
            _LOGGER.debug("Marstek command: %s", context)
        error = "API returned failure (request failed or set_result missing/false)"
        try:
            success = bool(await self.hass.async_add_executor_job(command, *args))
        except Exception as err:  # noqa: BLE001 - Keep maintenance alive on API failure.
            success = False
            error = f"{type(err).__name__}: {err}"

        if success:
            if source != "keepalive":
                _LOGGER.debug("Marstek command succeeded: %s", context)
        else:
            next_action = (
                f"retry in {PASSIVE_POWER_RETRY_SECONDS}s"
                if mode == MODE_PASSIVE
                else "passive control stopped; no automatic retry"
            )
            _LOGGER.warning(
                "Marstek command failed: %s error=%s; %s", context, error, next_action
            )
        return success

    async def _async_send_passive_power(
        self, outcome_state: str, *, source: str
    ) -> bool:
        """Send the current command and replace the shared keepalive/retry timer.

        The compensated command power goes on the wire, never the desired output.
        """
        if self._passive_command_power is None:
            return False

        # Also invalidate callbacks which have fired but are waiting for the lock.
        self._cancel_passive_keepalive()
        success = await self._async_send_mode_command(
            mode=MODE_PASSIVE, power=self._passive_command_power, source=source
        )
        self._passive_last_send_ok = success
        self._schedule_passive_keepalive(
            delay=(
                PASSIVE_POWER_KEEPALIVE_SECONDS
                if success
                else PASSIVE_POWER_RETRY_SECONDS
            ),
            source="keepalive" if success else "keepalive_retry",
        )
        if success:
            self._set_passive_power_state(outcome_state)
        return success

    def _schedule_passive_keepalive(self, *, delay: int, source: str) -> None:
        """Replace the one command-only timer after either success or failure."""
        self._cancel_passive_keepalive()
        generation = self._passive_control_generation

        async def async_keepalive(_now: datetime) -> None:
            """Keep HA's timer dispatch on the event loop."""
            await self._async_keepalive_passive_power(generation, source=source)

        self._passive_keepalive_cancel = async_call_later(
            self.hass, delay, async_keepalive
        )

    def _cancel_passive_keepalive(self) -> None:
        """Cancel the timer and invalidate any already-queued callback."""
        self._passive_control_generation += 1
        if self._passive_keepalive_cancel is not None:
            self._passive_keepalive_cancel()
            self._passive_keepalive_cancel = None

    def _clear_passive_control(self) -> None:
        """Discard the passive power target and invalidate queued callbacks.

        The learned calibration deliberately survives; only the live target does not.
        """
        self._passive_desired_power = None
        self._passive_command_power = None
        self._passive_command_source = SOURCE_DIRECT
        self._passive_last_send_ok = False
        self._passive_samples.clear()
        self._cancel_passive_keepalive()
        self._set_passive_power_state(PASSIVE_STATE_UNKNOWN)

    async def _async_keepalive_passive_power(
        self, generation: int, *, source: str
    ) -> None:
        """Resend the passive command power when its keepalive is due."""
        async with self._passive_command_lock:
            if (
                generation != self._passive_control_generation
                or self._passive_command_power is None
            ):
                return

            self._passive_keepalive_cancel = None
            await self._async_send_passive_power(PASSIVE_STATE_SENT, source=source)

    def _passive_power_is_confirmed(self, mode_data: dict) -> bool:
        """Return whether fresh mode data confirms the desired real output.

        Compensation aims the measurement at the desired value, so confirmation
        keeps comparing against what was requested rather than what was sent.
        """
        if (
            self._passive_desired_power is None
            or mode_data.get("mode") != MODE_PASSIVE
        ):
            return False

        reported_power = mode_data.get("ongrid_power")
        if not isinstance(reported_power, (int, float)):
            return False

        tolerance = max(
            abs(self._passive_desired_power) * PASSIVE_POWER_TOLERANCE,
            PASSIVE_POWER_ZERO_TOLERANCE,
        )
        return abs(reported_power - self._passive_desired_power) <= tolerance

    def _log_passive_power(self, source: str, actual: float | None = None) -> None:
        """Trace the requested output, the value sent, and what was measured."""
        _LOGGER.debug(
            "Marstek passive power: device=%s device_id=%s desired=%sW command=%sW "
            "actual=%sW source=%s",
            self.entry.title,
            self.device_id,
            self._passive_desired_power,
            self._passive_command_power,
            "unknown" if actual is None else actual,
            source,
        )

    def _passive_measured_output(
        self, mode_data: dict, es_data: dict | None
    ) -> float | None:
        """Return the measured real output, preferring the energy system status."""
        for candidate in (
            (es_data or {}).get("ongrid_power"),
            mode_data.get("ongrid_power"),
        ):
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                return float(candidate)
        return None

    def _passive_sample_is_learnable(
        self,
        mode_data: dict,
        es_data: dict | None,
        battery_data: dict | None,
        fresh: frozenset[str],
    ) -> bool:
        """Return whether the buffered samples may update the calibration."""
        desired = self._passive_desired_power
        if desired is None or self._passive_command_power is None:
            return False
        if mode_data.get("mode") != MODE_PASSIVE:
            return False
        # A failed send is still being retried, so nothing has settled yet.
        if not self._passive_last_send_ok or self._identity_mismatch:
            return False
        if abs(desired) < PASSIVE_MIN_LEARN_POWER_W:
            return False
        # Never learn from the stale cache _fetch_section serves during misses.
        if "es_mode" not in fresh or (es_data is not None and "es" not in fresh):
            return False
        if monotonic() - self._passive_command_changed_at < PASSIVE_SETTLE_SECONDS:
            return False
        if len(self._passive_samples) < PASSIVE_STABILITY_SAMPLES:
            return False

        measured = [value for _, value in self._passive_samples]
        if max(measured) - min(measured) > PASSIVE_STABILITY_TOLERANCE_W:
            return False

        return self._passive_headroom_available(desired, battery_data)

    def _passive_headroom_available(
        self, desired: int, battery_data: dict | None
    ) -> bool:
        """Return whether the device is free to follow the command right now."""
        battery = battery_data or {}
        soc = battery.get("soc")
        charging = desired < 0
        if isinstance(soc, (int, float)) and not isinstance(soc, bool):
            if charging and soc >= PASSIVE_SOC_LEARN_MAX:
                return False
            if not charging and soc <= PASSIVE_SOC_LEARN_MIN:
                return False

        # A device refusing the requested direction is constrained, not miscalibrated.
        flag = battery.get("charg_flag" if charging else "dischrg_flag")
        other = battery.get("dischrg_flag" if charging else "charg_flag")
        if flag is False and other is True:
            return False

        return True

    async def _async_verify_passive_power(
        self,
        mode_data: dict,
        *,
        es_data: dict | None = None,
        battery_data: dict | None = None,
        fresh: frozenset[str] = frozenset({"es_mode"}),
    ) -> None:
        """Confirm the desired output, compensate for it, or retry the command."""
        async with self._passive_command_lock:
            if self._passive_desired_power is None:
                return

            confirmed = self._passive_power_is_confirmed(mode_data)
            if confirmed:
                self._set_passive_power_state(PASSIVE_STATE_ACKNOWLEDGED)

            actual = self._passive_measured_output(mode_data, es_data)
            if actual is not None:
                self._passive_samples.append((monotonic(), actual))

            if actual is not None and await self._async_compensate(
                actual, mode_data, es_data, battery_data, fresh
            ):
                return

            if confirmed:
                return

            _LOGGER.debug(
                "Marstek passive verification mismatch: device=%s desired=%sW "
                "command=%sW reported_mode=%s reported_power=%sW",
                self.entry.title,
                self._passive_desired_power,
                self._passive_command_power,
                mode_data.get("mode"),
                mode_data.get("ongrid_power"),
            )
            await self._async_send_passive_power(
                PASSIVE_STATE_RETRYING, source="verification_retry"
            )

    async def _async_compensate(
        self,
        actual: float,
        mode_data: dict,
        es_data: dict | None,
        battery_data: dict | None,
        fresh: frozenset[str],
    ) -> bool:
        """Learn from a settled sample and resend when the command should move.

        Returns whether a compensating command was sent, so the caller can skip
        the verification retry that would otherwise duplicate it.
        """
        if not self._passive_sample_is_learnable(
            mode_data, es_data, battery_data, fresh
        ):
            return False

        desired = self._passive_desired_power
        command = self._passive_command_power
        error = desired - actual
        bucket = bucket_center(desired)
        key = (direction_of(desired), bucket)

        if abs(error) <= PASSIVE_DEADBAND_W:
            # Record that this bucket needs no compensation, so a restart does
            # not have to rediscover it, but leave a known value alone.
            if self.calibration.command_for(desired)[1] != SOURCE_CALIBRATION:
                self.calibration.observe(desired, command, actual)
                self._schedule_calibration_save()
            self._passive_saturated_buckets.discard(key)
            return False

        # Pinned against a device limit and still short: nothing more to learn.
        if self.calibration.is_saturated(command) and (error > 0) == (desired > 0):
            self._log_passive_power(SOURCE_SATURATED, actual)
            if key not in self._passive_saturated_buckets:
                self._passive_saturated_buckets.add(key)
                _LOGGER.warning(
                    "Marstek passive power saturated: device=%s device_id=%s "
                    "desired=%sW command=%sW actual=%sW; the requested output "
                    "cannot be reached",
                    self.entry.title,
                    self.device_id,
                    desired,
                    command,
                    actual,
                )
            return False

        self._passive_saturated_buckets.discard(key)
        result = self.calibration.observe(desired, command, actual)
        if result.changed:
            self._schedule_calibration_save()

        updated, _ = self.calibration.command_for(desired)
        if abs(updated - command) < PASSIVE_RESEND_THRESHOLD_W:
            return False

        self._set_passive_command_power(updated, SOURCE_COMPENSATION)
        self._log_passive_power(SOURCE_COMPENSATION, actual)
        await self._async_send_passive_power(
            PASSIVE_STATE_SENT, source=SOURCE_COMPENSATION
        )
        return True

    def _schedule_calibration_save(self) -> None:
        """Persist the calibration soon, coalescing rapid updates."""
        if self._calibration_store is not None:
            self._calibration_store.async_delay_save(
                self.calibration_snapshot, PASSIVE_SAVE_DELAY_SECONDS
            )

    async def _fetch_section(self, key: str, fetcher):
        """Fetch one section and track consecutive misses."""
        if key in self._disabled_optional_sections:
            return None

        if (
            key == "wifi"
            and key in self._last_good_data
            and monotonic() < self._next_wifi_poll_at
        ):
            return self._last_good_data[key]

        result = await self.hass.async_add_executor_job(fetcher)

        if key == "em" and isinstance(result, dict) and result.get("ct_state") == 0:
            self._disabled_optional_sections.add(key)
            self._last_good_data.pop(key, None)
            self._missing_cycles.pop(key, None)
            _LOGGER.info(
                "Marstek energy meter reports CT disconnected; skipping it until "
                "integration reload or Home Assistant restart: device=%s",
                self.entry.title,
            )
            return None

        if result is not None:
            self._missing_cycles[key] = 0
            self._last_good_data[key] = result
            if key == "wifi":
                # Only success delays the next poll; failures retry next cycle.
                self._next_wifi_poll_at = monotonic() + WIFI_POLL_INTERVAL_SECONDS
            return result

        self._missing_cycles[key] = self._missing_cycles.get(key, 0) + 1
        if key in OPTIONAL_SECTIONS:
            self._disabled_optional_sections.add(key)
            self._last_good_data.pop(key, None)
            _LOGGER.info(
                "Marstek optional section %s failed; skipping it until "
                "integration reload or Home Assistant restart: device=%s",
                key,
                self.entry.title,
            )
            return None

        use_cached = self._missing_cycles[key] <= 6 and key in self._last_good_data
        _LOGGER.debug(
            "Marstek section %s unavailable this cycle (miss %s; using_cached=%s)",
            key,
            self._missing_cycles[key],
            use_cached,
        )

        # Keep essential data for six missed cycles; pacing/timeouts extend each cycle.
        if use_cached:
            return self._last_good_data[key]

        return None

    async def _async_update_data(self):
        """Update data via library."""
        try:
            data = {}

            await self._async_refresh_device_info()
            data["device_info"] = dict(self.device_info)

            wifi_status = await self._fetch_section("wifi", self.api.get_wifi_status)
            if wifi_status is not None:
                data["wifi"] = wifi_status

            ble_status = await self._fetch_section("ble", self.api.get_ble_status)
            if ble_status is not None:
                data["ble"] = ble_status

            bat_status = await self._fetch_section(
                "battery", self.api.get_battery_status
            )
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
                    await self._async_verify_passive_power(
                        es_mode,
                        es_data=data.get("es"),
                        battery_data=data.get("battery"),
                        fresh=frozenset(
                            key
                            for key, misses in self._missing_cycles.items()
                            if misses == 0
                        ),
                    )

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
