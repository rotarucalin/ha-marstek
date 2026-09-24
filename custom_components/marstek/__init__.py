"""The Marstek Battery System integration."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .capabilities import MarstekCapabilities, resolve_capabilities
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
from .passive_telemetry import PassiveTelemetry
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


@dataclass
class PassiveCommandRetry:
    """The single recovery callback and the failed command it belongs to."""

    sequence: int
    desired_w: int
    command_w: int
    source: str
    cancel: Callable[[], None] | None = None


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
        # Resolved from cached metadata so entity platforms can gate on it
        # during the first setup, then refreshed once the device answers.
        self._capabilities = resolve_capabilities(self.device_info.get("device"))
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
        self._passive_retry: PassiveCommandRetry | None = None
        self._passive_command_in_flight = False
        self._passive_target_generation = 0
        self._passive_last_success_sequence = 0
        self._passive_command_lock = asyncio.Lock()
        self._passive_power_state = PASSIVE_STATE_UNKNOWN
        self._passive_send_sequence = 0
        self._passive_last_send_at = 0.0
        self._last_passive_command: dict | None = None
        self._passive_poll_active = False
        self._passive_charge_recovery_blocked = False
        self._calibration_store = store
        # The option is the ceiling the user asked for; the model's own
        # hardware rating (capabilities.passive_power_range) can only tighten
        # it further, never loosen it. Kept so a later capability refresh can
        # recompute the effective range without forgetting this input.
        self._configured_max_passive_power = max_passive_power
        command_min, command_max = self._capabilities.passive_power_range(
            self._configured_max_passive_power
        )
        self.calibration = PassiveCalibration.from_dict(
            calibration_data,
            command_min=command_min,
            command_max=command_max,
        )

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=SCAN_INTERVAL,
        )

    def _apply_passive_power_range(self) -> None:
        """Recompute the effective Passive/Manual command range and re-clamp.

        Called on init and whenever the capability resolution changes (a
        cached guess replaced by a live-confirmed model). The configured Max
        Passive Power option can only tighten this model's own hardware
        ceiling further, never loosen it.
        """
        command_min, command_max = self._capabilities.passive_power_range(
            self._configured_max_passive_power
        )
        self.calibration.set_command_range(command_min, command_max)

    @property
    def capabilities(self) -> MarstekCapabilities:
        """Return what the connected model supports.

        Platforms and command paths consult this instead of testing the
        reported model name, so a new model only needs a table entry.
        """
        return self._capabilities

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
            self._capabilities = resolve_capabilities(self.device_info.get("device"))
            self._apply_passive_power_range()
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
        # A poll publishes its data and verification state together. In particular,
        # recovery must not notify listeners with the pre-command zero reading.
        if not self._passive_poll_active:
            self.async_update_listeners()

    def _set_passive_command_power(self, command: int, source: str) -> None:
        """Adopt a new outgoing value and restart its measurement window."""
        self._passive_command_power = command
        self._passive_command_source = source
        self._passive_command_changed_at = monotonic()
        self._passive_samples.clear()

    async def async_set_passive_power(self, power: int) -> bool:
        """Set and maintain a desired real output for passive mode."""
        desired = max(
            self.calibration.command_min,
            min(self.calibration.command_max, power),
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
        # Do not restart Passive control when HA requests the target that is
        # already being maintained. In particular, leave an existing retry or
        # keepalive timer untouched.
        #
        # Only use this fast path while the command lock is free. If it is
        # locked, another target may already be in flight or waiting to replace
        # the currently recorded desired value.
        if (
            desired == self._passive_desired_power
            and not self._passive_command_lock.locked()
        ):
            _LOGGER.debug(
                "Marstek passive duplicate target ignored: device=%s "
                "desired=%sW state=%s",
                self.entry.title,
                desired,
                self._passive_power_state,
            )
            return self._passive_last_send_ok

        # Invalidate queued recovery before waiting for an in-flight command.
        # Its completion must not start recovery for the superseded target.
        self._passive_target_generation += 1
        generation = self._passive_target_generation
        self._cancel_passive_retry()
        self._cancel_passive_keepalive()
        async with self._passive_command_lock:
            if generation != self._passive_target_generation:
                return False
            if self._identity_mismatch:
                _LOGGER.warning(
                    "Marstek command blocked: device=%s source=new_target "
                    "mode=Passive power=%sW error=device identity mismatch",
                    self.entry.title,
                    power,
                )
                return False
            self._passive_desired_power = desired
            self._passive_charge_recovery_blocked = False
            command, source = self.calibration.command_for(desired)
            self._set_passive_command_power(command, source)
            self._log_passive_power(source)
            return await self._async_send_passive_power(
                PASSIVE_STATE_SENT, source="new_target"
            )

    async def async_set_operating_mode(
        self, mode: str, *, source: str = "operating_mode_select"
    ) -> bool:
        """Set an operating mode, superseding any passive power target.

        Manual has no safe parameterless form: every command addresses one
        schedule slot, so a mode change alone would have to either invent a
        schedule or silently overwrite whatever is already in a slot. Use
        `async_set_manual_schedule` (the `marstek.set_operating_mode_manual`
        service) instead, which always requires the caller to state a slot.
        """
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
            if not self.capabilities.supports_mode(mode):
                _LOGGER.warning(
                    "Marstek command blocked: device=%s source=%s mode=%s "
                    "error=%s does not support this mode",
                    self.entry.title,
                    source,
                    mode,
                    self.capabilities.model,
                )
                return False
            if mode == MODE_MANUAL:
                _LOGGER.warning(
                    "Marstek command blocked: device=%s source=%s mode=%s "
                    "error=Manual has no default schedule; call the "
                    "marstek.set_operating_mode_manual service with an "
                    "explicit time_num instead",
                    self.entry.title,
                    source,
                    mode,
                )
                return False
            if mode == MODE_PASSIVE:
                return True

            self._clear_passive_control()

            return await self._async_send_mode_command(mode=mode, source=source)

    async def async_set_manual_schedule(
        self,
        *,
        time_num: int,
        start_time: str,
        end_time: str,
        week_set: int,
        power: int,
        enable: int,
        manual_set: int | None = None,
        source: str = "set_operating_mode_manual",
    ) -> bool:
        """Write exactly one Manual schedule slot, superseding Passive control.

        Every field is caller-supplied. Nothing here is invented or read back
        from an existing slot; the caller (the `marstek.set_operating_mode_manual`
        service) owns choosing a safe schedule.

        Passive state is only torn down once the device has actually accepted
        the Manual command. A failed attempt must not stop the countdown
        keepalive for a Passive session the device is still physically in.
        """
        async with self._passive_command_lock:
            if self._identity_mismatch:
                _LOGGER.warning(
                    "Marstek command blocked: device=%s source=%s mode=%s "
                    "error=device identity mismatch",
                    self.entry.title,
                    source,
                    MODE_MANUAL,
                )
                return False
            if not self.capabilities.supports_mode(MODE_MANUAL):
                _LOGGER.warning(
                    "Marstek command blocked: device=%s source=%s mode=%s "
                    "error=%s does not support this mode",
                    self.entry.title,
                    source,
                    MODE_MANUAL,
                    self.capabilities.model,
                )
                return False
            if not self.capabilities.is_valid_manual_slot(time_num):
                _LOGGER.warning(
                    "Marstek command blocked: device=%s source=%s mode=%s "
                    "error=%s has no Manual slot %s",
                    self.entry.title,
                    source,
                    MODE_MANUAL,
                    self.capabilities.model,
                    time_num,
                )
                return False

            # _async_send_mode_command's sequence/timing/last-command
            # bookkeeping is shared with Passive's own retry/verification
            # staleness and settle-window checks. Capture it so a failed
            # attempt (which must not touch Passive state at all) can be
            # undone rather than leaking into them.
            sequence_before = self._passive_send_sequence
            last_command_before = self._last_passive_command
            last_send_at_before = self._passive_last_send_at

            success = await self._async_send_mode_command(
                mode=MODE_MANUAL,
                source=source,
                manual_params={
                    "time_num": time_num,
                    "start_time": start_time,
                    "end_time": end_time,
                    "week_set": week_set,
                    "power": power,
                    "enable": enable,
                    "manual_set": manual_set,
                },
            )

            if success:
                # Only now has the device actually left Passive mode: discard
                # the old mode's keepalive/retry and requested-power tracking.
                self._clear_passive_control()
            else:
                self._passive_send_sequence = sequence_before
                self._last_passive_command = last_command_before
                self._passive_last_send_at = last_send_at_before

            return success

    async def async_stop_passive_control(self) -> None:
        """Stop maintaining passive mode power."""
        async with self._passive_command_lock:
            self._clear_passive_control()

    async def _async_send_mode_command(
        self,
        *,
        mode: str,
        source: str,
        power: int | None = None,
        manual_params: dict | None = None,
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
        elif mode == MODE_MANUAL and manual_params is not None:
            args = (
                manual_params["time_num"],
                manual_params["start_time"],
                manual_params["end_time"],
                manual_params["week_set"],
                manual_params["power"],
                manual_params["enable"],
            )
            # manual_cfg only accepts manual_set on the Venus E mini, where it
            # selects the slot's direction; other models reject the field.
            manual_set = manual_params.get("manual_set")
            if manual_set is not None:
                args += (manual_set,)
            command = self.api.set_es_mode_manual
        else:
            return False

        context = (
            f"device={self.entry.title} device_id={self.device_id} "
            f"host={self.entry.data['host']} port={self.entry.data.get('port', 30000)} "
            f"source={source} method=ES.SetMode mode={mode}"
        )
        if power is not None:
            context += f" power={power}W"
        if mode == MODE_MANUAL and manual_params is not None:
            context += (
                f" time_num={manual_params['time_num']} "
                f"start_time={manual_params['start_time']} "
                f"end_time={manual_params['end_time']} "
                f"week_set={manual_params['week_set']} "
                f"power={manual_params['power']}W "
                f"enable={manual_params['enable']}"
            )
            if manual_params.get("manual_set") is not None:
                context += f" manual_set={manual_params['manual_set']}"
        if source != "keepalive":
            _LOGGER.debug("Marstek command: %s", context)
        error = "API returned failure (request failed or set_result missing/false)"
        self._passive_send_sequence += 1
        self._last_passive_command = {
            "sequence": self._passive_send_sequence,
            "time": datetime.now(UTC).isoformat(),
            "source": source,
            "mode": mode,
            "desired_w": self._passive_desired_power,
            "command_w": power if manual_params is None else manual_params.get("power"),
            "success": None,
        }
        try:
            success = bool(await self.hass.async_add_executor_job(command, *args))
        except Exception as err:  # noqa: BLE001 - Keep maintenance alive on API failure.
            success = False
            error = f"{type(err).__name__}: {err}"

        self._passive_last_send_at = monotonic()
        self._last_passive_command["success"] = success

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
        """Send the current command and schedule either keepalive or recovery.

        The compensated command power goes on the wire, never the desired output.
        """
        if self._passive_command_power is None:
            return False

        # Also invalidate callbacks which have fired but are waiting for the lock.
        self._cancel_passive_keepalive()
        self._cancel_passive_retry()
        generation = self._passive_target_generation
        self._passive_command_in_flight = True
        try:
            success = await self._async_send_mode_command(
                mode=MODE_PASSIVE, power=self._passive_command_power, source=source
            )
        finally:
            self._passive_command_in_flight = False
        if success:
            self._passive_last_success_sequence = self._passive_send_sequence
        if generation != self._passive_target_generation:
            return success
        self._passive_last_send_ok = success
        if success:
            self._schedule_passive_keepalive(
                delay=PASSIVE_POWER_KEEPALIVE_SECONDS, source="keepalive"
            )
            self._set_passive_power_state(outcome_state)
        else:
            self._passive_samples.clear()
            self._schedule_passive_retry(source)
        return success

    def _log_passive_recovery(
        self, event: str, retry: PassiveCommandRetry | None = None
    ) -> None:
        """Include the failed command's original provenance in recovery logs."""
        retry = retry or self._passive_retry
        command = self._last_passive_command or {}
        source = retry.source if retry else command.get("source", "unknown")
        if source != "verification_retry":
            source = source.removesuffix("_retry")
        _LOGGER.debug(
            "Marstek passive %s: device=%s sequence=%s desired_w=%s "
            "command_w=%s original_source=%s",
            event,
            self.entry.title,
            retry.sequence if retry else self._passive_send_sequence,
            retry.desired_w if retry else self._passive_desired_power,
            retry.command_w if retry else self._passive_command_power,
            source,
        )

    def _passive_recovery_pending(self, source: str) -> bool:
        """Suppress other command producers while recovery owns the command."""
        if self._passive_command_in_flight or self._passive_retry is not None:
            self._log_passive_recovery(
                f"{source} suppressed because recovery is pending"
            )
            return True
        return False

    def _schedule_passive_retry(self, source: str) -> None:
        """Schedule just one retry, preserving the origin across failures."""
        if self._passive_retry is not None:
            return
        retry = PassiveCommandRetry(
            self._passive_send_sequence,
            self._passive_desired_power,
            self._passive_command_power,
            source if source == "verification_retry" else source.removesuffix("_retry"),
        )
        self._passive_retry = retry

        async def async_retry(_now: datetime) -> None:
            await self._async_retry_passive_power(retry)

        retry.cancel = async_call_later(
            self.hass, PASSIVE_POWER_RETRY_SECONDS, async_retry
        )
        self._log_passive_recovery("retry scheduled", retry)

    def _cancel_passive_retry(self) -> None:
        """Cancel recovery, including a callback already waiting for the lock."""
        if (retry := self._passive_retry) is None:
            return
        self._passive_retry = None
        if retry.cancel is not None:
            retry.cancel()
        self._log_passive_recovery("retry cancelled", retry)

    async def _async_retry_passive_power(self, retry: PassiveCommandRetry) -> None:
        """Only the active failed command may consume the recovery slot."""
        async with self._passive_command_lock:
            if (
                retry is not self._passive_retry
                or retry.sequence != self._passive_send_sequence
                or retry.desired_w != self._passive_desired_power
                or retry.command_w != self._passive_command_power
                or self._passive_last_success_sequence > retry.sequence
            ):
                self._log_passive_recovery("stale retry discarded", retry)
                return
            self._passive_retry = None
            # Failed sends and their observations must not contribute samples
            # to calibration after recovery succeeds.
            self._passive_samples.clear()
            await self._async_send_passive_power(
                PASSIVE_STATE_SENT,
                source=(
                    retry.source
                    if retry.source.endswith("_retry")
                    else f"{retry.source}_retry"
                ),
            )
            # The network request itself can outlast the settle period.
            self._passive_command_changed_at = self._passive_last_send_at

    def _schedule_passive_keepalive(self, *, delay: int, source: str) -> None:
        """Restart normal maintenance after a successful command."""
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
        self._passive_target_generation += 1
        self._cancel_passive_retry()
        self._passive_desired_power = None
        self._passive_command_power = None
        self._passive_command_source = SOURCE_DIRECT
        self._passive_last_send_ok = False
        self._passive_charge_recovery_blocked = False
        self._passive_samples.clear()
        self._passive_send_sequence += 1
        self._cancel_passive_keepalive()
        self._set_passive_power_state(PASSIVE_STATE_UNKNOWN)

    async def _async_keepalive_passive_power(
        self, generation: int, *, source: str
    ) -> None:
        """Resend the passive command power when its keepalive is due."""
        if self._passive_recovery_pending("keepalive"):
            return
        async with self._passive_command_lock:
            if self._passive_recovery_pending("keepalive"):
                return
            if (
                generation != self._passive_control_generation
                or self._passive_command_power is None
            ):
                return

            self._passive_keepalive_cancel = None
            await self._async_send_passive_power(PASSIVE_STATE_SENT, source=source)

    def _passive_power_is_confirmed(self, telemetry: PassiveTelemetry) -> bool:
        """Return whether fresh mode data confirms the desired real output.

        Compensation aims the measurement at the desired value, so confirmation
        keeps comparing against what was requested rather than what was sent.
        """
        if (
            self._passive_desired_power is None
            or telemetry.mode.get("mode") != MODE_PASSIVE
        ):
            return False

        reported_power = telemetry.actual
        if reported_power is None:
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
        if not {"es_mode", "es", "battery"}.issubset(fresh):
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
        return flag not in (False, 0)

    async def _async_verify_passive_power(
        self,
        mode_data: dict,
        *,
        es_data: dict | None = None,
        battery_data: dict | None = None,
        fresh: frozenset[str] = frozenset({"es_mode"}),
        command_sequence: int | None = None,
        confirmed_drop: bool = False,
        allow_send: bool = True,
    ) -> bool:
        """Confirm the desired output, compensate for it, or retry the command."""
        # Check before locking so an observation made during ES.SetMode cannot
        # wait for its completion and then start a second recovery operation.
        if self._passive_recovery_pending("verification"):
            return False
        generation = self._passive_target_generation
        if command_sequence is None:
            command_sequence = self._passive_send_sequence
        async with self._passive_command_lock:
            if self._passive_recovery_pending("verification"):
                return False
            if self._passive_desired_power is None:
                return False
            if (
                generation != self._passive_target_generation
                or command_sequence != self._passive_send_sequence
            ):
                self._passive_samples.clear()
                return False

            telemetry = PassiveTelemetry(
                mode_data, es_data or {}, battery_data or {}, fresh
            )
            actual = telemetry.actual
            if actual is None:
                self._passive_samples.clear()
                self._set_passive_power_state(PASSIVE_STATE_UNKNOWN)
                return False

            interrupted = telemetry.unexpected_zero(self._passive_desired_power)
            if interrupted:
                self._passive_samples.clear()
                self._set_passive_power_state(PASSIVE_STATE_UNKNOWN)
                if self._passive_desired_power < 0 and not telemetry.charging_permitted:
                    if confirmed_drop or not allow_send:
                        self._passive_charge_recovery_blocked = True
                        self._cancel_passive_keepalive()
                        self._log_passive_observation("recovery_blocked", telemetry)
                    return False
                if not confirmed_drop:
                    return False
                if monotonic() - self._passive_last_send_at < PASSIVE_SETTLE_SECONDS:
                    return False

            confirmed = self._passive_power_is_confirmed(telemetry)
            if confirmed:
                self._set_passive_power_state(PASSIVE_STATE_ACKNOWLEDGED)
                if (
                    self._passive_desired_power < 0
                    and self._passive_charge_recovery_blocked
                    and telemetry.charging_permitted
                ):
                    self._passive_charge_recovery_blocked = False
                    self._schedule_passive_keepalive(
                        delay=PASSIVE_POWER_KEEPALIVE_SECONDS, source="keepalive"
                    )
            else:
                self._set_passive_power_state(PASSIVE_STATE_UNKNOWN)

            if not allow_send:
                # Post-command reads only verify. Never start an unbounded chain
                # of retries or learn from a recovery/settling observation.
                return False

            if not interrupted:
                self._passive_samples.append((monotonic(), actual))

            if not interrupted and await self._async_compensate(
                actual, mode_data, es_data, battery_data, fresh
            ):
                return True

            if confirmed:
                return False

            if self._passive_desired_power < 0:
                if not telemetry.charging_permitted:
                    return False
                self._passive_charge_recovery_blocked = False

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
            return True

    def _log_passive_observation(self, phase: str, telemetry: PassiveTelemetry) -> None:
        """Log the evidence, including successful keepalives normally hidden."""
        _LOGGER.debug(
            "Marstek passive observation: %s",
            json.dumps(
                {
                    "phase": phase,
                    "device": self.entry.title,
                    "device_id": self.device_id,
                    "time": datetime.now(UTC).isoformat(),
                    "desired_w": self._passive_desired_power,
                    "command_w": self._passive_command_power,
                    "verification_state": self._passive_power_state,
                    "endpoint_disagreement": telemetry.disagreement,
                    "preceding_command": self._last_passive_command,
                    "seconds_since_command": (
                        round(monotonic() - self._passive_last_send_at, 3)
                        if self._last_passive_command
                        else None
                    ),
                    **telemetry.diagnostic_data(),
                },
                sort_keys=True,
            ),
        )

    async def _async_read_passive_telemetry(
        self, data: dict
    ) -> tuple[int, PassiveTelemetry]:
        """Replace the poll's readings with a paced, bounded follow-up round."""
        sequence = self._passive_send_sequence
        fresh = set()
        for key, fetcher in (
            ("battery", self.api.get_battery_status),
            ("es", self.api.get_es_status),
            ("es_mode", self.api.get_es_mode),
        ):
            # Do not reuse pre-command readings, even when the follow-up fails.
            data.pop(key, None)
            result = await self._fetch_section(key, fetcher)
            if self._missing_cycles.get(key) == 0 and result is not None:
                data[key] = result
                fresh.add(key)
            else:
                self._last_good_data.pop(key, None)
        return sequence, PassiveTelemetry(
            data.get("es_mode", {}),
            data.get("es", {}),
            data.get("battery", {}),
            frozenset(fresh),
        )

    async def _async_wait_passive_settle(self) -> None:
        """Allow recovery to settle without blocking newer targets or mode changes."""
        remaining = PASSIVE_SETTLE_SECONDS - (monotonic() - self._passive_last_send_at)
        if remaining > 0:
            await asyncio.sleep(remaining)

    def _discard_superseded_passive_readings(self, data: dict) -> None:
        """Do not publish a power snapshot spanning two different commands."""
        self._passive_samples.clear()
        for key in ("es", "es_mode"):
            data.pop(key, None)
            self._last_good_data.pop(key, None)

    async def _async_process_passive_telemetry(self, data: dict, sequence: int) -> None:
        """Confirm interruptions once and publish only post-recovery telemetry."""
        if self._passive_desired_power is None:
            return
        fresh = frozenset(
            key
            for key in ("battery", "es", "es_mode")
            if key in data and self._missing_cycles.get(key) == 0
        )
        telemetry = PassiveTelemetry(
            data.get("es_mode", {}),
            data.get("es", {}),
            data.get("battery", {}),
            fresh,
        )
        if sequence != self._passive_send_sequence:
            self._discard_superseded_passive_readings(data)
            return
        suspect = telemetry.disagreement or telemetry.unexpected_zero(
            self._passive_desired_power
        )
        if suspect:
            self._passive_samples.clear()
            self._set_passive_power_state(PASSIVE_STATE_UNKNOWN)
            self._log_passive_observation("suspected_interruption", telemetry)
            if monotonic() - self._passive_last_send_at < PASSIVE_SETTLE_SECONDS:
                await self._async_wait_passive_settle()
            sequence, telemetry = await self._async_read_passive_telemetry(data)
            self._log_passive_observation("confirmation", telemetry)

        sent = await self._async_verify_passive_power(
            telemetry.mode,
            es_data=telemetry.es,
            battery_data=telemetry.battery,
            fresh=telemetry.fresh,
            command_sequence=sequence,
            confirmed_drop=suspect,
        )
        if not sent:
            if sequence != self._passive_send_sequence:
                self._discard_superseded_passive_readings(data)
            return

        # A recovery command cannot make the previously collected data current.
        # Leave the command lock free during settling so stops/new targets win.
        await self._async_wait_passive_settle()
        sequence, telemetry = await self._async_read_passive_telemetry(data)
        await self._async_verify_passive_power(
            telemetry.mode,
            es_data=telemetry.es,
            battery_data=telemetry.battery,
            fresh=telemetry.fresh,
            command_sequence=sequence,
            allow_send=False,
        )
        self._log_passive_observation("post_recovery", telemetry)
        if sequence != self._passive_send_sequence:
            self._discard_superseded_passive_readings(data)

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
        self._passive_poll_active = True
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

            passive_sequence = self._passive_send_sequence
            bat_status = await self._fetch_section(
                "battery", self.api.get_battery_status
            )
            if bat_status is not None:
                data["battery"] = bat_status

            # Only Venus A/D answer PV.GetStatus; the rest would time out here
            # every cycle until the optional-section probe gave up.
            if self.capabilities.supports_pv:
                pv_status = await self._fetch_section("pv", self.api.get_pv_status)
                if pv_status is not None:
                    data["pv"] = pv_status

            es_status = await self._fetch_section("es", self.api.get_es_status)
            if es_status is not None:
                data["es"] = es_status

            es_mode = await self._fetch_section("es_mode", self.api.get_es_mode)
            if es_mode is not None:
                data["es_mode"] = es_mode

            await self._async_process_passive_telemetry(data, passive_sequence)

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
        finally:
            self._passive_poll_active = False
