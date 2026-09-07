"""Service handlers for the Marstek integration."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
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

        # Find coordinator through loaded config entries
        domain_data = hass.data.get(DOMAIN, {})
        coordinator = None

        for entry_id, entry_coordinator in domain_data.items():
            try:
                select_entity_id = f"select.{entry_coordinator.data['device_info']['device'].lower()}_battery_system_operating_mode"
            except Exception:
                select_entity_id = None

            # Fallback: just accept first coordinator if only one device exists
            if len(domain_data) == 1:
                coordinator = entry_coordinator
                break

            # If you want exact entity_id mapping, improve this part later
            if select_entity_id == entity_id:
                coordinator = entry_coordinator
                break

        if coordinator is None:
            _LOGGER.error("Could not resolve Marstek device for entity_id=%s", entity_id)
            return

        if not await coordinator.async_set_passive_power(power):
            _LOGGER.error(
                "Failed to set passive mode for %s: power=%s",
                entity_id,
                power,
            )

    if not hass.services.has_service(DOMAIN, SERVICE_SET_OPERATING_MODE_PASSIVE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_OPERATING_MODE_PASSIVE,
            async_handle_set_operating_mode_passive,
            schema=SERVICE_SET_OPERATING_MODE_PASSIVE_SCHEMA,
        )