"""ES solar-energy scaling and Bat.GetStatus permission-flag binary sensors."""

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.marstek.const import DOMAIN
from custom_components.marstek.sensor import _es_total_pv_energy_kwh

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def entity_id(hass, entry, platform, key):
    """Look up an entity by its stable hardware identity."""
    found = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{entry.unique_id}_{key}"
    )
    assert found is not None, key
    return found


def state(hass, entry, platform, key):
    found = hass.states.get(entity_id(hass, entry, platform, key))
    assert found is not None, key
    return found


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"total_pv_energy": 100}, 1.0),
        ({"total_pv_energy": 1}, 0.01),
        ({"total_pv_energy": 12345}, 123.45),
        ({"total_pv_energy": 0}, 0),
        ({"total_pv_energy": None}, None),
        ({}, None),
    ],
)
def test_es_total_pv_energy_scaling(data, expected):
    """total_pv_energy is documented in 0.01 kWh per count."""
    assert _es_total_pv_energy_kwh(data) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(100, "1.0"), (1, "0.01"), (None, "unknown")],
)
async def test_es_total_pv_energy_entity(
    hass, marstek_entry, mock_marstek_api, raw, expected
):
    """The existing ES solar-energy entity publishes kWh with energy metadata."""
    mock_marstek_api.get_es_status.return_value = {"total_pv_energy": raw}
    await setup(hass, marstek_entry)
    sensor = state(hass, marstek_entry, "sensor", "es_total_pv_energy")
    assert sensor.state == expected
    assert sensor.attributes["unit_of_measurement"] == "kWh"
    assert sensor.attributes["device_class"] == "energy"
    assert sensor.attributes["state_class"] == "total_increasing"


async def test_es_total_pv_energy_unavailable_without_es(
    hass, marstek_entry, mock_marstek_api
):
    mock_marstek_api.get_es_status.return_value = None
    await setup(hass, marstek_entry)
    assert (
        state(hass, marstek_entry, "sensor", "es_total_pv_energy").state
        == "unavailable"
    )


@pytest.mark.parametrize(
    ("battery", "charging", "discharging"),
    [
        ({"soc": 50, "charg_flag": True, "dischrg_flag": True}, "on", "on"),
        ({"soc": 50, "charg_flag": False, "dischrg_flag": True}, "off", "on"),
        ({"soc": 50, "charg_flag": True, "dischrg_flag": False}, "on", "off"),
        ({"soc": 50, "charg_flag": False, "dischrg_flag": False}, "off", "off"),
        # Section present but flag missing: unchanged existing behavior.
        ({"soc": 50}, "unknown", "unknown"),
        # Section missing: unavailable.
        (None, "unavailable", "unavailable"),
    ],
)
async def test_permission_flags(
    hass, marstek_entry, mock_marstek_api, battery, charging, discharging
):
    """charg_flag/dischrg_flag map directly to Charging/Discharging Allowed."""
    mock_marstek_api.get_battery_status.return_value = battery
    await setup(hass, marstek_entry)
    allowed_charge = state(hass, marstek_entry, "binary_sensor", "battery_charging")
    allowed_discharge = state(
        hass, marstek_entry, "binary_sensor", "battery_discharging"
    )
    assert allowed_charge.state == charging
    assert allowed_discharge.state == discharging


async def test_permission_flag_metadata(hass, marstek_entry, mock_marstek_api):
    """Permission sensors keep their unique IDs but imply no battery activity."""
    mock_marstek_api.get_battery_status.return_value = {
        "soc": 50,
        "charg_flag": True,
        "dischrg_flag": True,
    }
    await setup(hass, marstek_entry)
    registry = er.async_get(hass)
    for key, name in (
        ("battery_charging", "Charging Allowed"),
        ("battery_discharging", "Discharging Allowed"),
    ):
        found = state(hass, marstek_entry, "binary_sensor", key)
        assert "device_class" not in found.attributes
        assert found.attributes["friendly_name"].endswith(name)
        entry = registry.async_get(found.entity_id)
        assert entry.unique_id == f"{marstek_entry.unique_id}_{key}"
    assert (
        len(
            [
                entry
                for entry in er.async_entries_for_config_entry(
                    registry, marstek_entry.entry_id
                )
                if entry.domain == "binary_sensor"
            ]
        )
        == 4
    )
