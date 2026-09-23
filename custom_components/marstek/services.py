"""Service handlers for the Marstek integration."""

from __future__ import annotations

import logging
import re

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import EntityComponent

from .const import DOMAIN, MANUAL_SET_AUTO, MANUAL_SET_DISABLE, MANUAL_SLOTS_DEFAULT

_LOGGER = logging.getLogger(__name__)

SERVICE_SET_OPERATING_MODE_PASSIVE = "set_operating_mode_passive"
SERVICE_SET_OPERATING_MODE_MANUAL = "set_operating_mode_manual"

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _validate_hhmm(value: object) -> str:
    """Validate an `HH:MM` string as manual_cfg's start_time/end_time expect."""
    if not isinstance(value, str) or not _HHMM.match(value):
        raise vol.Invalid(f"'{value}' is not a valid HH:MM time")
    return value


SERVICE_SET_OPERATING_MODE_PASSIVE_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Required("power"): vol.Coerce(int),
        vol.Required("cd_time"): vol.All(vol.Coerce(int), vol.Range(min=1, max=86400)),
    }
)

# time_num's generic bound (chapter 3.6's manual_cfg table: Venus A/C/D/E use
# 0-9). The Venus E mini's tighter 0-5 range is model-specific, so it is
# checked against the resolved device's own capabilities, not here.
SERVICE_SET_OPERATING_MODE_MANUAL_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Required("time_num"): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=MANUAL_SLOTS_DEFAULT - 1)
        ),
        vol.Required("start_time"): _validate_hhmm,
        vol.Required("end_time"): _validate_hhmm,
        vol.Required("week_set"): vol.All(vol.Coerce(int), vol.Range(min=0, max=127)),
        vol.Required("power"): vol.Coerce(int),
        vol.Required("enable"): cv.boolean,
        vol.Optional("manual_set"): vol.All(
            vol.Coerce(int), vol.Range(min=MANUAL_SET_DISABLE, max=MANUAL_SET_AUTO)
        ),
    }
)


def _resolve_coordinator(hass: HomeAssistant, entity_id: str):
    """Return the coordinator owning `entity_id`, raising if it cannot be found.

    Kept as the one lookup both services share, so `entity_id` always resolves
    to a device the same way regardless of which service was called.
    """
    coordinator = None
    entity_entry = er.async_get(hass).async_get(entity_id)
    if (
        entity_entry is not None
        and entity_entry.platform == DOMAIN
        and entity_entry.config_entry_id is not None
    ):
        coordinator = hass.data.get(DOMAIN, {}).get(entity_entry.config_entry_id)

    if coordinator is None:
        _LOGGER.error(
            "Could not resolve Marstek device for entity_id=%s",
            entity_id,
        )
        raise ServiceValidationError(
            f"Could not resolve a Marstek device for entity_id={entity_id}"
        )

    return coordinator


async def async_register_services(
    hass: HomeAssistant,
    component: EntityComponent | None = None,
) -> None:
    """Register Marstek services."""

    async def async_handle_set_operating_mode_passive(call: ServiceCall) -> None:
        entity_id = call.data["entity_id"]
        power = call.data["power"]

        coordinator = _resolve_coordinator(hass, entity_id)

        if not await coordinator.async_set_passive_power(power):
            raise HomeAssistantError(
                f"Marstek could not set Passive power for entity_id={entity_id}"
            )

    async def async_handle_set_operating_mode_manual(call: ServiceCall) -> None:
        entity_id = call.data["entity_id"]
        time_num = call.data["time_num"]
        power = call.data["power"]
        manual_set = call.data.get("manual_set")

        coordinator = _resolve_coordinator(hass, entity_id)
        capabilities = coordinator.capabilities

        if not capabilities.is_valid_manual_slot(time_num):
            raise ServiceValidationError(
                f"{capabilities.model} does not have a Manual slot {time_num}; "
                f"valid slots are 0-{capabilities.manual_slots - 1}"
            )

        if capabilities.supports_manual_set and manual_set is None:
            raise ServiceValidationError(
                f"{capabilities.model} requires manual_set for a Manual command "
                f"({MANUAL_SET_DISABLE}=disable, {MANUAL_SET_DISABLE + 1}=charge, "
                f"{MANUAL_SET_DISABLE + 2}=discharge, {MANUAL_SET_AUTO}=auto)"
            )
        if not capabilities.supports_manual_set and manual_set is not None:
            raise ServiceValidationError(
                f"{capabilities.model} does not accept manual_set"
            )

        min_power, max_power = capabilities.passive_power_range()
        if not min_power <= power <= max_power:
            raise ServiceValidationError(
                f"power={power}W is outside the {capabilities.model} limit of "
                f"{min_power}W..{max_power}W"
            )

        success = await coordinator.async_set_manual_schedule(
            time_num=time_num,
            start_time=call.data["start_time"],
            end_time=call.data["end_time"],
            week_set=call.data["week_set"],
            power=power,
            enable=int(call.data["enable"]),
            manual_set=manual_set,
        )
        if not success:
            raise HomeAssistantError(
                f"Marstek could not set the Manual schedule for entity_id={entity_id}"
            )
        await coordinator.async_request_refresh()

    if not hass.services.has_service(DOMAIN, SERVICE_SET_OPERATING_MODE_PASSIVE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_OPERATING_MODE_PASSIVE,
            async_handle_set_operating_mode_passive,
            schema=SERVICE_SET_OPERATING_MODE_PASSIVE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SET_OPERATING_MODE_MANUAL):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_OPERATING_MODE_MANUAL,
            async_handle_set_operating_mode_manual,
            schema=SERVICE_SET_OPERATING_MODE_MANUAL_SCHEMA,
        )
