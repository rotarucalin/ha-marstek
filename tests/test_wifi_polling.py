"""Wi-Fi diagnostics use long normal and failure-backoff intervals."""

from unittest.mock import Mock

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.marstek import MarstekDataUpdateCoordinator
from custom_components.marstek.const import DOMAIN
from custom_components.marstek.polling import ENDPOINTS

pytestmark = pytest.mark.asyncio
_, WIFI_INTERVAL, WIFI_CAP = ENDPOINTS["wifi"]


@pytest.fixture
def wifi_clock(monkeypatch):
    clock = Mock(return_value=1000.0)
    monkeypatch.setattr("custom_components.marstek.monotonic", clock)
    return clock


@pytest.fixture
def coordinator(hass, marstek_entry, mock_marstek_api, wifi_clock):
    marstek_entry.add_to_hass(hass)
    return MarstekDataUpdateCoordinator(hass, mock_marstek_api, marstek_entry)


def polls_at(times, interval):
    """Reads a completion-deadline endpoint makes at these (instant) poll times."""
    count, last = 0, None
    for now in times:
        if last is None or now >= last + interval:
            count, last = count + 1, now
    return count


@pytest.mark.parametrize("response", [{"rssi": -45}, {}])
async def test_wifi_waits_its_interval_while_other_endpoints_keep_polling(
    coordinator, mock_marstek_api, wifi_clock, response
):
    """Initial reads are immediate; cached diagnostics do not add UDP requests."""
    mock_marstek_api.get_wifi_status.return_value = response
    assert (await coordinator._async_update_data())["wifi"] == response
    mock_marstek_api.get_wifi_status.return_value = {"rssi": -65}
    times = [0, 30, 60, 120, 180, 240, 300, 600, WIFI_INTERVAL - 0.001]
    for seconds in times[1:]:
        wifi_clock.return_value = 1000 + seconds
        assert (await coordinator._async_update_data())["wifi"] == response
        assert mock_marstek_api.get_wifi_status.call_count == 1
        assert coordinator._missing_cycles["wifi"] == 0

    times.append(WIFI_INTERVAL)
    wifi_clock.return_value = 1000 + WIFI_INTERVAL
    assert (await coordinator._async_update_data())["wifi"] == {"rssi": -65}
    assert mock_marstek_api.get_wifi_status.call_count == 2
    for key, method in (
        ("ble", "get_ble_status"),
        ("battery", "get_battery_status"),
        ("pv", "get_pv_status"),
        ("es", "get_es_status"),
        ("es_mode", "get_es_mode"),
        ("em", "get_em_status"),
    ):
        expected = polls_at(times, ENDPOINTS[key][1])
        assert expected > 1
        assert getattr(mock_marstek_api, method).call_count == expected


async def test_initial_wifi_failures_back_off_until_success(
    coordinator, mock_marstek_api, wifi_clock
):
    mock_marstek_api.get_wifi_status.side_effect = [None, None, {"rssi": -60}]
    wifi = coordinator._polling.sections["wifi"]
    for seconds in (0, WIFI_CAP):
        wifi_clock.return_value = 1000 + seconds
        assert "wifi" not in await coordinator._async_update_data()
        assert wifi.next_poll == 1000 + seconds + WIFI_CAP
    wifi_clock.return_value = wifi.next_poll - 0.001
    assert "wifi" not in await coordinator._async_update_data()
    assert mock_marstek_api.get_wifi_status.call_count == 2
    success = wifi.next_poll
    wifi_clock.return_value = success
    assert (await coordinator._async_update_data())["wifi"] == {"rssi": -60}
    assert mock_marstek_api.get_wifi_status.call_count == 3
    assert "wifi" not in coordinator._disabled_optional_sections

    mock_marstek_api.get_wifi_status.side_effect = None
    mock_marstek_api.get_wifi_status.return_value = {"rssi": -50}
    wifi_clock.return_value = success + WIFI_INTERVAL - 0.001
    assert (await coordinator._async_update_data())["wifi"] == {"rssi": -60}
    assert mock_marstek_api.get_wifi_status.call_count == 3
    wifi_clock.return_value = success + WIFI_INTERVAL
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
    wifi_clock.return_value = 1000.0 + WIFI_INTERVAL
    await coordinator._async_update_data()
    assert mock_marstek_api.get_wifi_status.call_count == 1
    wifi_clock.return_value = 1005.0 + WIFI_INTERVAL
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
