"""The `marstek.set_operating_mode_manual` service the README already documented.

Every test drives the service exactly as an automation would, through
`hass.services.async_call`, so schema validation, coordinator resolution and
capability gating are all exercised together.
"""

from __future__ import annotations

import pytest
import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.marstek.const import DOMAIN, MANUAL_SET_AUTO
from custom_components.marstek.services import (
    SERVICE_SET_OPERATING_MODE_MANUAL,
    async_register_services,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("enable_custom_integrations")]


async def _setup_model(hass, entry, api, reported):
    """Set up a real config entry reporting one model."""
    api.get_device_info.return_value = {
        "device": reported,
        "ble_mac": "AA:BB:CC:DD:EE:01",
        "ver": 123,
    }
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await async_register_services(hass)
    return er.async_get(hass).async_get_entity_id(
        "select", DOMAIN, f"{entry.unique_id}_operating_mode"
    )


def _base_data(entity_id: str, **overrides) -> dict:
    data = {
        "entity_id": entity_id,
        "time_num": 0,
        "start_time": "08:00",
        "end_time": "20:00",
        "week_set": 127,
        "power": 300,
        "enable": True,
    }
    data.update(overrides)
    return data


async def _call_manual(hass, **data) -> None:
    await hass.services.async_call(
        DOMAIN, SERVICE_SET_OPERATING_MODE_MANUAL, data, blocking=True
    )


async def test_manual_service_is_registered(hass):
    await async_register_services(hass)
    assert hass.services.has_service(DOMAIN, SERVICE_SET_OPERATING_MODE_MANUAL)


async def test_manual_service_sends_normal_device_payload(
    hass, marstek_entry, mock_marstek_api
):
    """A Venus D command carries exactly the six documented fields."""
    entity_id = await _setup_model(hass, marstek_entry, mock_marstek_api, "Venus D")
    mock_marstek_api.set_es_mode_manual.return_value = True

    await _call_manual(
        hass,
        **_base_data(entity_id, time_num=3, power=500, week_set=3),
    )

    mock_marstek_api.set_es_mode_manual.assert_called_once_with(
        3, "08:00", "20:00", 3, 500, 1
    )


async def test_manual_service_sends_manual_set_on_e_mini(
    hass, marstek_entry, mock_marstek_api
):
    """The E mini's payload carries the required manual_set."""
    entity_id = await _setup_model(hass, marstek_entry, mock_marstek_api, "VNSEM-0")
    mock_marstek_api.set_es_mode_manual.return_value = True

    await _call_manual(
        hass,
        **_base_data(entity_id, time_num=2, manual_set=MANUAL_SET_AUTO),
    )

    mock_marstek_api.set_es_mode_manual.assert_called_once_with(
        2, "08:00", "20:00", 127, 300, 1, MANUAL_SET_AUTO
    )


async def test_manual_service_triggers_a_refresh_on_success(
    hass, marstek_entry, mock_marstek_api
):
    entity_id = await _setup_model(hass, marstek_entry, mock_marstek_api, "Venus D")
    mock_marstek_api.set_es_mode_manual.return_value = True
    mock_marstek_api.get_es_mode.return_value = {"mode": "Manual"}

    await _call_manual(hass, **_base_data(entity_id))
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state.state == "Manual"


async def test_manual_service_rejects_e_mini_slot_beyond_range(
    hass, marstek_entry, mock_marstek_api
):
    entity_id = await _setup_model(hass, marstek_entry, mock_marstek_api, "VNSEM-0")

    with pytest.raises(ServiceValidationError):
        await _call_manual(
            hass, **_base_data(entity_id, time_num=6, manual_set=MANUAL_SET_AUTO)
        )

    mock_marstek_api.set_es_mode_manual.assert_not_called()


async def test_manual_service_requires_manual_set_on_e_mini(
    hass, marstek_entry, mock_marstek_api
):
    entity_id = await _setup_model(hass, marstek_entry, mock_marstek_api, "VNSEM-0")

    with pytest.raises(ServiceValidationError):
        await _call_manual(hass, **_base_data(entity_id))

    mock_marstek_api.set_es_mode_manual.assert_not_called()


@pytest.mark.parametrize("reported", ["Venus A", "Venus C", "Venus D", "Venus E"])
async def test_manual_service_rejects_manual_set_on_other_models(
    hass, marstek_entry, mock_marstek_api, reported
):
    entity_id = await _setup_model(hass, marstek_entry, mock_marstek_api, reported)

    with pytest.raises(ServiceValidationError):
        await _call_manual(
            hass,
            **_base_data(entity_id, power=100, manual_set=MANUAL_SET_AUTO),
        )

    mock_marstek_api.set_es_mode_manual.assert_not_called()


async def test_manual_service_rejects_power_beyond_the_model_limit(
    hass, marstek_entry, mock_marstek_api
):
    """Venus A's own 1500 W ceiling rejects a 3000 W slot outright."""
    entity_id = await _setup_model(hass, marstek_entry, mock_marstek_api, "Venus A")

    with pytest.raises(ServiceValidationError):
        await _call_manual(hass, **_base_data(entity_id, power=3000))

    mock_marstek_api.set_es_mode_manual.assert_not_called()


async def test_manual_service_raises_when_the_device_rejects_the_command(
    hass, marstek_entry, mock_marstek_api
):
    entity_id = await _setup_model(hass, marstek_entry, mock_marstek_api, "Venus D")
    mock_marstek_api.set_es_mode_manual.return_value = False

    with pytest.raises(HomeAssistantError):
        await _call_manual(hass, **_base_data(entity_id))

    mock_marstek_api.set_es_mode_manual.assert_called_once()


async def test_manual_service_raises_for_an_unresolved_entity(hass):
    await async_register_services(hass)

    with pytest.raises(ServiceValidationError):
        await _call_manual(hass, **_base_data("select.unknown_marstek_entity"))


@pytest.mark.parametrize("bad_time", ["8:00", "24:00", "12:60", "noon", ""])
async def test_manual_service_rejects_malformed_times(
    hass, marstek_entry, mock_marstek_api, bad_time
):
    entity_id = await _setup_model(hass, marstek_entry, mock_marstek_api, "Venus D")

    with pytest.raises(vol.Invalid):
        await _call_manual(hass, **_base_data(entity_id, start_time=bad_time))

    mock_marstek_api.set_es_mode_manual.assert_not_called()
