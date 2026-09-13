"""Service handlers for the Marstek integration."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import EntityComponent

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SERVICE_SET_OPERATING_MODE_PASSIVE = "set_operating_mode_passive"

SERVICE_SET_OPERATING_MODE_PASSIVE_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Required("power"): vol.Coerce(int),
        vol.Required("cd_time"): vol.All(vol.Coerce(int), vol.Range(min=1, max=86400)),
    }
)


async def async_register_services(
    hass: HomeAssistant,
    component: EntityComponent | None = None,
) -> None:
    """Register Marstek services."""

    async def async_handle_set_operating_mode_passive(call: ServiceCall) -> None:
        entity_id = call.data["entity_id"]
        power = call.data["power"]

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
            return

        await coordinator.async_set_passive_power(power)

    if not hass.services.has_service(DOMAIN, SERVICE_SET_OPERATING_MODE_PASSIVE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_OPERATING_MODE_PASSIVE,
            async_handle_set_operating_mode_passive,
            schema=SERVICE_SET_OPERATING_MODE_PASSIVE_SCHEMA,
        )
