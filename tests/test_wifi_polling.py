"""Wi-Fi diagnostics poll slowly after success and recover at normal cadence."""

from unittest.mock import Mock

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.marstek import MarstekDataUpdateCoordinator
from custom_components.marstek.const import DOMAIN

pytestmark = pytest.mark.asyncio


@pytest.fixture
def wifi_clock(monkeypatch):
    clock = Mock(return_value=1000.0)
    monkeypatch.setattr("custom_components.marstek.monotonic", clock)
    return clock


@pytest.fixture
def coordinator(hass, marstek_entry, mock_marstek_api, wifi_clock):
    marstek_entry.add_to_hass(hass)
    return MarstekDataUpdateCoordinator(hass, mock_marstek_api, marstek_entry)


@pytest.mark.parametrize("response", [{"rssi": -45}, {}])
async def test_wifi_waits_five_minutes_while_other_endpoints_keep_polling(
    coordinator, mock_marstek_api, wifi_clock, response
):
    """Initial reads are immediate; cached diagnostics do not add UDP requests."""
    mock_marstek_api.get_wifi_status.return_value = response
    assert (await coordinator._async_update_data())["wifi"] == response
    mock_marstek_api.get_wifi_status.return_value = {"rssi": -65}
    for seconds in (30, 60, 120, 180, 240, 299.999):
        wifi_clock.return_value = 1000 + seconds
        assert (await coordinator._async_update_data())["wifi"] == response
        assert mock_marstek_api.get_wifi_status.call_count == 1
        assert coordinator._missing_cycles["wifi"] == 0

    wifi_clock.return_value = 1300.0
    assert (await coordinator._async_update_data())["wifi"] == {"rssi": -65}
    assert mock_marstek_api.get_wifi_status.call_count == 2
    for method in (
        "get_ble_status",
        "get_battery_status",
        "get_pv_status",
        "get_es_status",
        "get_es_mode",
        "get_em_status",
    ):
        assert getattr(mock_marstek_api, method).call_count == 8


async def test_initial_wifi_failures_retry_each_cycle_until_success(
    coordinator, mock_marstek_api, wifi_clock
):
    mock_marstek_api.get_wifi_status.side_effect = [None, None, {"rssi": -60}]
    for seconds in (0, 30):
        wifi_clock.return_value = 1000 + seconds
        assert "wifi" not in await coordinator._async_update_data()
    wifi_clock.return_value = 1060.0
    assert (await coordinator._async_update_data())["wifi"] == {"rssi": -60}
    assert mock_marstek_api.get_wifi_status.call_count == 3
    assert "wifi" not in coordinator._disabled_optional_sections

    mock_marstek_api.get_wifi_status.side_effect = None
    mock_marstek_api.get_wifi_status.return_value = {"rssi": -50}
    wifi_clock.return_value = 1359.999
    assert (await coordinator._async_update_data())["wifi"] == {"rssi": -60}
    assert mock_marstek_api.get_wifi_status.call_count == 3
    wifi_clock.return_value = 1360.0
    assert (await coordinator._async_update_data())["wifi"] == {"rssi": -50}
    assert mock_marstek_api.get_wifi_status.call_count == 4


async def test_wifi_interval_starts_after_successful_response(
    coordinator, mock_marstek_api, wifi_clock
):
    def receive():
        wifi_clock.return_value += 5
        return {"rssi": -45}

    mock_marstek_api.get_wifi_status.side_effect = receive
    await coordinator._async_update_data()
    wifi_clock.return_value = 1300.0
    await coordinator._async_update_data()
    assert mock_marstek_api.get_wifi_status.call_count == 1
    wifi_clock.return_value = 1305.0
    await coordinator._async_update_data()
    assert mock_marstek_api.get_wifi_status.call_count == 2


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_reload_refreshes_wifi_immediately(
    hass, marstek_entry, mock_marstek_api, wifi_clock
):
    marstek_entry.add_to_hass(hass)
    mock_marstek_api.get_wifi_status.return_value = {"rssi": -45}
    assert await hass.config_entries.async_setup(marstek_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][marstek_entry.entry_id]
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{marstek_entry.unique_id}_wifi_rssi"
    )
    assert hass.states.get(entity_id).state == "-45"

    mock_marstek_api.get_wifi_status.return_value = {"rssi": -65}
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert mock_marstek_api.get_wifi_status.call_count == 1
    assert hass.states.get(entity_id).state == "-45"

    assert await hass.config_entries.async_reload(marstek_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.data[DOMAIN][marstek_entry.entry_id] is not coordinator
    assert mock_marstek_api.get_wifi_status.call_count == 2
    assert hass.states.get(entity_id).state == "-65"
