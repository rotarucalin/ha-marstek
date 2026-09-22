"""PV.GetStatus channel readings, metadata and availability in Home Assistant."""

import json
from unittest.mock import patch

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.marstek.const import DOMAIN
from custom_components.marstek.marstek_api import MarstekAPI

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def setup_pv(hass, entry, api, payload):
    """Set up actual sensor entities with one PV.GetStatus result."""
    api.get_pv_status.return_value = payload
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return hass.data[DOMAIN][entry.entry_id]


def sensor_state(hass, entry, key):
    """Look up a sensor by its stable hardware identity."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.unique_id}_{key}"
    )
    assert entity_id is not None, key
    state = hass.states.get(entity_id)
    assert state is not None, key
    return state


async def test_four_pv_channels(hass, marstek_entry, mock_marstek_api, pv_status):
    """Each input has its own values and units; power is an explicit sum."""
    mock_marstek_api.get_es_status.return_value = {"total_pv_energy": 4321}
    await setup_pv(hass, marstek_entry, mock_marstek_api, pv_status)
    for channel in range(1, 5):
        for field, unit in (("power", "W"), ("voltage", "V"), ("current", "A")):
            key = f"pv{channel}_{field}"
            state = sensor_state(hass, marstek_entry, key)
            assert float(state.state) == pv_status[key]
            assert state.attributes["unit_of_measurement"] == unit
            assert state.attributes["device_class"] == field
            assert state.attributes["state_class"] == "measurement"
        state = sensor_state(hass, marstek_entry, f"pv{channel}_state")
        assert state.state == ("standby" if channel == 4 else "working")
        assert state.attributes["device_class"] == "enum"
        assert state.attributes["options"] == ["standby", "working"]
        assert "unit_of_measurement" not in state.attributes
        assert "state_class" not in state.attributes

    power = sensor_state(hass, marstek_entry, "pv_power")
    assert float(power.state) == 377.5
    assert power.attributes["unit_of_measurement"] == "W"
    assert power.attributes["device_class"] == "power"
    assert power.attributes["state_class"] == "measurement"
    energy = sensor_state(hass, marstek_entry, "pv_total_pv_energy")
    assert float(energy.state) == 12500
    assert energy.attributes["unit_of_measurement"] == "Wh"
    assert energy.attributes["device_class"] == "energy"
    assert energy.attributes["state_class"] == "total_increasing"
    # ES.GetStatus has its own energy total; preserve its source and identity.
    assert sensor_state(hass, marstek_entry, "es_total_pv_energy").state == "4321"
    for key in ("pv_voltage", "pv_current"):
        assert (
            er.async_get(hass).async_get_entity_id(
                "sensor", DOMAIN, f"{marstek_entry.unique_id}_{key}"
            )
            is None
        )


async def test_partial_channels(hass, marstek_entry, mock_marstek_api, pv_status):
    """Nonconsecutive inputs work; absent inputs never appear as zero."""
    payload = {
        key: value
        for key, value in pv_status.items()
        if not key.startswith(("pv2_", "pv4_"))
    }
    await setup_pv(hass, marstek_entry, mock_marstek_api, payload)
    for channel in (1, 3):
        for field in ("power", "voltage", "current"):
            key = f"pv{channel}_{field}"
            assert float(sensor_state(hass, marstek_entry, key).state) == payload[key]
        assert (
            sensor_state(hass, marstek_entry, f"pv{channel}_state").state == "working"
        )
    for channel in (2, 4):
        for field in ("power", "voltage", "current", "state"):
            assert (
                sensor_state(hass, marstek_entry, f"pv{channel}_{field}").state
                == "unavailable"
            )
    assert float(sensor_state(hass, marstek_entry, "pv_power").state) == 202.5


@pytest.mark.parametrize("missing", ["omitted", "null"])
async def test_channel_data_disappears_and_recovers(
    hass, marstek_entry, mock_marstek_api, pv_status, missing
):
    """Availability follows each reading on refresh without stale or zero data."""
    coordinator = await setup_pv(hass, marstek_entry, mock_marstek_api, pv_status)
    payload = dict(pv_status)
    missing_keys = [
        "pv1_power",
        "pv2_voltage",
        "pv3_current",
        "pv3_state",
        "pv4_power",
        "pv4_voltage",
        "pv4_current",
        "pv4_state",
        "total_pv_energy",
    ]
    for key in missing_keys:
        if missing == "null":
            payload[key] = None
        else:
            payload.pop(key)
    mock_marstek_api.get_pv_status.return_value = payload
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    for key in missing_keys:
        sensor_key = "pv_total_pv_energy" if key == "total_pv_energy" else key
        assert sensor_state(hass, marstek_entry, sensor_key).state == "unavailable"
    assert sensor_state(hass, marstek_entry, "pv1_voltage").state == "30"
    assert sensor_state(hass, marstek_entry, "pv2_power").state == "175"
    assert float(sensor_state(hass, marstek_entry, "pv_power").state) == 257.5

    mock_marstek_api.get_pv_status.return_value = pv_status
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert sensor_state(hass, marstek_entry, "pv1_power").state == "120"
    assert sensor_state(hass, marstek_entry, "pv4_power").state == "0"
    assert sensor_state(hass, marstek_entry, "pv4_state").state == "standby"
    assert sensor_state(hass, marstek_entry, "pv_total_pv_energy").state == "12500"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (None, "unavailable"),
        ({}, "unavailable"),
        ({"id": 0, "total_pv_energy": 100}, "unavailable"),
        (
            {
                "pv1_power": None,
                "pv2_power": None,
                "pv3_power": None,
                "pv4_power": None,
            },
            "unavailable",
        ),
        ({"pv1_power": 0, "pv2_power": None}, "0"),
        ({"pv1_power": None, "pv4_power": 25}, "25"),
        ({"pv1_power": 0, "pv2_power": 0, "pv3_power": 0, "pv4_power": 0}, "0"),
        # Legacy generic fields must never feed PV sensors or the sum.
        ({"pv_power": 999, "pv_voltage": 30, "pv_current": 4}, "unavailable"),
        ({"pv_power": 999, "pv1_power": 12}, "12"),
    ],
)
async def test_aggregate_available_readings(
    hass, marstek_entry, mock_marstek_api, payload, expected
):
    await setup_pv(hass, marstek_entry, mock_marstek_api, payload)
    assert sensor_state(hass, marstek_entry, "pv_power").state == expected


async def test_unknown_channel_state(hass, marstek_entry, mock_marstek_api):
    """An undocumented status must not be mislabeled as standby or working."""
    await setup_pv(hass, marstek_entry, mock_marstek_api, {"pv1_state": 2})
    assert sensor_state(hass, marstek_entry, "pv1_state").state == "unavailable"


def test_pv_api_response(pv_status):
    """The real UDP client preserves the numbered PV.GetStatus result."""
    api = MarstekAPI("192.0.2.1")
    with patch("custom_components.marstek.marstek_api.socket.socket") as socket:
        connection = socket.return_value.__enter__.return_value
        connection.recvfrom.return_value = (
            json.dumps({"id": 1, "src": "VenusA-mac", "result": pv_status}).encode(),
            ("192.0.2.1", 30000),
        )
        assert api.get_pv_status() == pv_status
        payload, address = connection.sendto.call_args.args
        assert address == ("192.0.2.1", 30000)
        assert json.loads(payload) == {
            "id": 1,
            "method": "PV.GetStatus",
            "params": {"id": 0},
        }
