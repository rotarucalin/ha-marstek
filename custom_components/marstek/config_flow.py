"""Config flow for Marstek Battery System integration."""

from __future__ import annotations

import logging
from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import DEFAULT_PORT, DOMAIN
from .identity import CONF_DEVICE_INFO, device_metadata, normalize_mac
from .marstek_api import MarstekAPI

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    api = MarstekAPI(host=data[CONF_HOST], port=data.get(CONF_PORT, DEFAULT_PORT))

    # Try to get device info
    device_info = await hass.async_add_executor_job(api.get_device_info)

    if not isinstance(device_info, dict) or not device_info:
        raise CannotConnect

    ble_mac = normalize_mac(device_info.get("ble_mac"))
    if ble_mac is None:
        raise InvalidIdentity

    # Return info that you want to store in the config entry.
    return {
        "title": f"Marstek {device_info.get('device', 'Device')}",
        "ble_mac": ble_mac,
        CONF_DEVICE_INFO: {**device_metadata(device_info), "ble_mac": ble_mac},
    }


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Marstek Battery System."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidIdentity:
                errors["base"] = "invalid_identity"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                # Create a unique ID based on the BLE MAC address
                await self.async_set_unique_id(info["ble_mac"])
                self._abort_if_unique_id_configured()
                # Legacy entries used the API's original MAC spelling.
                if any(
                    normalize_mac(entry.unique_id) == info["ble_mac"]
                    for entry in self._async_current_entries()
                ):
                    return self.async_abort(reason="already_configured")

                return self.async_create_entry(
                    title=info["title"],
                    data={**user_input, CONF_DEVICE_INFO: info[CONF_DEVICE_INFO]},
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )


class CannotConnect(Exception):
    """Error to indicate we cannot connect."""


class InvalidIdentity(Exception):
    """Error to indicate the device did not report a usable BLE MAC."""
