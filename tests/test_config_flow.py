"""Tests for validated, persistent identities during configuration."""

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.marstek.const import DOMAIN
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
