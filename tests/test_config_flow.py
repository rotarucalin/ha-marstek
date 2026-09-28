"""Tests for validated, persistent identities during configuration."""

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.marstek.const import (
    CONF_MAX_PASSIVE_POWER,
    CONF_PASSIVE_KEEPALIVE_SECONDS,
    DEFAULT_PASSIVE_KEEPALIVE_SECONDS,
    DOMAIN,
)
from custom_components.marstek.identity import CONF_DEVICE_INFO

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.usefixtures("enable_custom_integrations"),
]


async def test_flow_persists_identity_and_metadata(hass, mock_marstek_api):
    """Configuration stores hardware metadata together with connection settings."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
        data={"host": "192.0.2.1", "port": 30000},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "aa:bb:cc:dd:ee:01"
    assert result["data"] == {
        "host": "192.0.2.1",
        "port": 30000,
        CONF_DEVICE_INFO: {
            "device": "Venus A",
            "ble_mac": "aa:bb:cc:dd:ee:01",
            "ver": 123,
        },
    }
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    "mac", [None, "", "unknown", "not-a-mac", "00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"]
)
async def test_flow_rejects_invalid_identity(hass, mock_marstek_api, mac):
    """A nonempty API response is insufficient without a usable hardware identity."""
    mock_marstek_api.get_device_info.return_value = {
        "device": "Venus A",
        "ble_mac": mac,
    }
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
        data={"host": "192.0.2.1", "port": 30000},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_identity"}
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.parametrize(
    "mac", ["aa:bb:cc:dd:ee:01", "AABBCCDDEE01", "aa-bb-cc-dd-ee-01"]
)
async def test_flow_rejects_duplicate_with_different_mac_format(
    hass,
    marstek_entry,
    mock_marstek_api,
    mac,
):
    """A changed address or MAC spelling must not create a second config entry."""
    marstek_entry.add_to_hass(hass)
    mock_marstek_api.get_device_info.return_value = {
        "device": "Venus A",
        "ble_mac": mac,
    }
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
        data={"host": "192.0.2.99", "port": 30000},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_flow_accepts_second_battery_of_same_model(
    hass, marstek_entry, mock_marstek_api
):
    """The hardware identity, not the model name, distinguishes batteries."""
    marstek_entry.add_to_hass(hass)
    mock_marstek_api.get_device_info.return_value = {
        "device": "Venus A",
        "ble_mac": "AA:BB:CC:DD:EE:02",
    }
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
        data={"host": "192.0.2.2", "port": 30000},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "aa:bb:cc:dd:ee:02"
    await hass.async_block_till_done()


@pytest.mark.parametrize("configured", [None, 60, 600, 3500])
async def test_passive_keepalive_option_schema(hass, marstek_entry, configured):
    """Existing entries default to 180 seconds; the form retains saved values."""
    marstek_entry.add_to_hass(hass)
    if configured is not None:
        hass.config_entries.async_update_entry(
            marstek_entry, options={CONF_PASSIVE_KEEPALIVE_SECONDS: configured}
        )
    result = await hass.config_entries.options.async_init(marstek_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    schema = result["data_schema"]
    assert schema({})[CONF_PASSIVE_KEEPALIVE_SECONDS] == (
        DEFAULT_PASSIVE_KEEPALIVE_SECONDS if configured is None else configured
    )
    selector = schema.schema[CONF_PASSIVE_KEEPALIVE_SECONDS]
    assert selector.config == {
        "min": 60, "max": 3500, "step": 60,
        "unit_of_measurement": "s", "mode": "box",
    }
    for value in (60, 180, 600, 3500):
        assert schema({CONF_PASSIVE_KEEPALIVE_SECONDS: value})[
            CONF_PASSIVE_KEEPALIVE_SECONDS
        ] == value
    for value in (59, 3501):
        with pytest.raises(vol.Invalid):
            schema({CONF_PASSIVE_KEEPALIVE_SECONDS: value})


async def test_passive_keepalive_default_and_options_reload(
    hass, marstek_entry, mock_marstek_api
):
    """Setup defaults to 180 seconds; saving the option reloads the coordinator."""
    marstek_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(marstek_entry.entry_id)
    await hass.async_block_till_done()
    original = hass.data[DOMAIN][marstek_entry.entry_id]
    assert original._passive_keepalive_seconds == 180

    result = await hass.config_entries.options.async_init(marstek_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_MAX_PASSIVE_POWER: 1000, CONF_PASSIVE_KEEPALIVE_SECONDS: 600},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    reloaded = hass.data[DOMAIN][marstek_entry.entry_id]
    assert reloaded is not original
    assert reloaded._passive_keepalive_seconds == 600
    assert reloaded.calibration.command_max == 1000
    assert await hass.config_entries.async_unload(marstek_entry.entry_id)
