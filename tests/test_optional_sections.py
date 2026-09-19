"""Optional endpoint failures are latched only for the coordinator's lifetime."""

import logging

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.marstek import MarstekDataUpdateCoordinator
from custom_components.marstek.const import DOMAIN

pytestmark = pytest.mark.asyncio

OPTIONAL = {"ble": "get_ble_status", "pv": "get_pv_status"}
ESSENTIAL = {
    "wifi": "get_wifi_status",
    "battery": "get_battery_status",
    "es": "get_es_status",
    "es_mode": "get_es_mode",
    "em": "get_em_status",
}


@pytest.fixture
def coordinator(hass, marstek_entry, mock_marstek_api):
    marstek_entry.add_to_hass(hass)
    return MarstekDataUpdateCoordinator(hass, mock_marstek_api, marstek_entry)


@pytest.mark.parametrize("section", OPTIONAL)
async def test_first_optional_failure_stops_queries(
    coordinator, mock_marstek_api, caplog, section
):
    """Only the failed optional endpoint stops; all other sections keep polling."""
    caplog.set_level(logging.INFO, logger="custom_components.marstek")
    fetcher = getattr(mock_marstek_api, OPTIONAL[section])
    fetcher.return_value = None
    for cycle in range(10):
        data = await coordinator._async_update_data()
        assert section not in data
        assert all(key in data for key in ESSENTIAL)
        # Even a subsequently responsive endpoint is not probed until reload.
        fetcher.return_value = {}
        assert fetcher.call_count == 1
        assert coordinator._disabled_optional_sections == {section}
        for method in ESSENTIAL.values():
            assert getattr(mock_marstek_api, method).call_count == cycle + 1
    other_section = ({"ble", "pv"} - {section}).pop()
    assert getattr(mock_marstek_api, OPTIONAL[other_section]).call_count == 10
    assert caplog.text.count("skipping it until integration reload") == 1


@pytest.mark.parametrize("section", OPTIONAL)
async def test_optional_failure_discards_last_good_reading(
    coordinator, mock_marstek_api, section
):
    fetcher = getattr(mock_marstek_api, OPTIONAL[section])
    fetcher.return_value = {"value": 12}
    assert (await coordinator._async_update_data())[section] == {"value": 12}
    fetcher.return_value = None
    assert section not in await coordinator._async_update_data()
    assert section not in coordinator._last_good_data
    assert section not in await coordinator._async_update_data()
    assert fetcher.call_count == 2


@pytest.mark.parametrize(
    ("section", "response"),
    [
        ("ble", {}),
        ("pv", {}),
        ("pv", {"pv_power": 0}),
        ("ble", {"state": "disconnect"}),
    ],
)
async def test_successful_optional_responses_remain_in_polling(
    coordinator, mock_marstek_api, section, response
):
    fetcher = getattr(mock_marstek_api, OPTIONAL[section])
    fetcher.return_value = response
    for _ in range(3):
        assert (await coordinator._async_update_data())[section] == response
    assert not coordinator._disabled_optional_sections
    assert fetcher.call_count == 3


@pytest.mark.parametrize("section", ESSENTIAL)
async def test_essential_endpoint_keeps_retrying_and_recovers(
    coordinator, mock_marstek_api, section
):
    """Essential data keeps its six-cycle cache and can recover after expiry."""
    for method in OPTIONAL.values():
        getattr(mock_marstek_api, method).return_value = None
    original = (await coordinator._async_update_data())[section]
    fetcher = getattr(mock_marstek_api, ESSENTIAL[section])
    fetcher.return_value = None
    for misses in range(1, 8):
        data = await coordinator._async_update_data()
        if misses <= 6:
            assert data[section] == original
        else:
            assert section not in data
        assert coordinator._disabled_optional_sections == {"ble", "pv"}
        assert fetcher.call_count == misses + 1
    fetcher.return_value = original
    assert (await coordinator._async_update_data())[section] == original
    assert coordinator._missing_cycles[section] == 0
    for method in OPTIONAL.values():
        assert getattr(mock_marstek_api, method).call_count == 1


async def test_total_outage_does_not_disable_essential_recovery(
    coordinator, mock_marstek_api
):
    """No optional failures may prevent essential retries after update failure."""
    for method in (*OPTIONAL.values(), *ESSENTIAL.values()):
        getattr(mock_marstek_api, method).return_value = None
    for _ in range(2):
        with pytest.raises(UpdateFailed, match="No Marstek data received"):
            await coordinator._async_update_data()
    for method in ESSENTIAL.values():
        assert getattr(mock_marstek_api, method).call_count == 2
        getattr(mock_marstek_api, method).return_value = {}
    assert set(await coordinator._async_update_data()) == {"device_info", *ESSENTIAL}
    for method in OPTIONAL.values():
        assert getattr(mock_marstek_api, method).call_count == 1


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_reload_reprobes_sections_and_restores_entity_availability(
    hass, marstek_entry, mock_marstek_api
):
    """Real entry reload replaces the flags and restores existing PV/BLE entities."""
    marstek_entry.add_to_hass(hass)
    mock_marstek_api.get_pv_status.return_value = {"pv_power": 0}
    mock_marstek_api.get_ble_status.return_value = {"state": "disconnect"}
    assert await hass.config_entries.async_setup(marstek_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][marstek_entry.entry_id]
    registry = er.async_get(hass)
    pv_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{marstek_entry.unique_id}_pv_power"
    )
    ble_id = registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{marstek_entry.unique_id}_bluetooth_connected"
    )
    assert hass.states.get(pv_id).state == "0"
    assert hass.states.get(ble_id).state == "off"

    for method in OPTIONAL.values():
        getattr(mock_marstek_api, method).return_value = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator._disabled_optional_sections == {"ble", "pv"}
    assert hass.states.get(pv_id).state == "unavailable"
    assert hass.states.get(ble_id).state == "unavailable"

    mock_marstek_api.get_pv_status.return_value = {"pv_power": 0}
    mock_marstek_api.get_ble_status.return_value = {"state": "disconnect"}
    await coordinator.async_refresh()
    for method in OPTIONAL.values():
        assert getattr(mock_marstek_api, method).call_count == 2

    assert await hass.config_entries.async_reload(marstek_entry.entry_id)
    await hass.async_block_till_done()
    replacement = hass.data[DOMAIN][marstek_entry.entry_id]
    assert replacement is not coordinator
    assert not replacement._disabled_optional_sections
    assert hass.states.get(pv_id).state == "0"
    assert hass.states.get(ble_id).state == "off"
    for method in OPTIONAL.values():
        assert getattr(mock_marstek_api, method).call_count == 3


@pytest.mark.parametrize("previously_connected", [False, True])
@pytest.mark.parametrize("disconnected", [0, False])
async def test_ct_disconnection_stops_meter_queries(
    coordinator, mock_marstek_api, caplog, previously_connected, disconnected
):
    """The first disconnected reading discards meter data and stops only EM."""
    caplog.set_level(logging.INFO, logger="custom_components.marstek")
    fetcher = mock_marstek_api.get_em_status
    connected = {"ct_state": 1, "total_power": 90, "a_power": 10}
    if previously_connected:
        fetcher.return_value = connected
        assert (await coordinator._async_update_data())["em"] == connected
        # A transient timeout can precede disconnection without disabling EM.
        fetcher.return_value = None
        assert (await coordinator._async_update_data())["em"] == connected

    preceding_calls = fetcher.call_count
    fetcher.return_value = {**connected, "ct_state": disconnected}
    for cycle in range(3):
        data = await coordinator._async_update_data()
        assert "em" not in data
        assert "em" not in coordinator._last_good_data
        assert "em" not in coordinator._missing_cycles
        assert coordinator._disabled_optional_sections == {"em"}
        assert fetcher.call_count == preceding_calls + 1
        # A reconnected meter is not probed until reload, even if now healthy.
        fetcher.return_value = connected
        for key, method in {**ESSENTIAL, **OPTIONAL}.items():
            if key != "em":
                assert key in data
                assert (
                    getattr(mock_marstek_api, method).call_count
                    == preceding_calls + cycle + 1
                )
    assert caplog.text.count("energy meter reports CT disconnected") == 1


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"ct_state": None},
        {"ct_state": 1},
        {"ct_state": True},
        {"ct_state": -1},
        {"ct_state": "0"},
    ],
)
async def test_meter_requires_explicit_disconnected_state_to_stop(
    coordinator, mock_marstek_api, response
):
    """Timeouts, connected readings and unknown state values keep EM polling."""
    mock_marstek_api.get_em_status.return_value = response
    for _ in range(3):
        data = await coordinator._async_update_data()
        assert not coordinator._disabled_optional_sections
        if response is not None:
            assert data["em"] == response
    assert mock_marstek_api.get_em_status.call_count == 3


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_reload_reprobes_disconnected_meter_and_restores_entities(
    hass, marstek_entry, mock_marstek_api
):
    """CT and all meter power entities become unavailable, then recover on reload."""
    marstek_entry.add_to_hass(hass)
    connected = {
        "ct_state": 1,
        "total_power": 90,
        "a_power": 10,
        "b_power": 30,
        "c_power": 50,
    }
    mock_marstek_api.get_em_status.return_value = connected
    assert await hass.config_entries.async_setup(marstek_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][marstek_entry.entry_id]
    registry = er.async_get(hass)
    states = {}
    for platform, key, expected in (
        ("binary_sensor", "ct_connected", "on"),
        ("sensor", "em_total_power", "90"),
        ("sensor", "em_phase_a_power", "10"),
        ("sensor", "em_phase_b_power", "30"),
        ("sensor", "em_phase_c_power", "50"),
    ):
        entity_id = registry.async_get_entity_id(
            platform, DOMAIN, f"{marstek_entry.unique_id}_{key}"
        )
        states[entity_id] = expected
        assert hass.states.get(entity_id).state == expected

    mock_marstek_api.get_em_status.return_value = {**connected, "ct_state": 0}
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    for entity_id in states:
        assert hass.states.get(entity_id).state == "unavailable"

    mock_marstek_api.get_em_status.return_value = connected
    await coordinator.async_refresh()
    assert mock_marstek_api.get_em_status.call_count == 2

    assert await hass.config_entries.async_reload(marstek_entry.entry_id)
    await hass.async_block_till_done()
    replacement = hass.data[DOMAIN][marstek_entry.entry_id]
    assert replacement is not coordinator
    assert not replacement._disabled_optional_sections
    assert mock_marstek_api.get_em_status.call_count == 3
    for entity_id, expected in states.items():
        assert hass.states.get(entity_id).state == expected
