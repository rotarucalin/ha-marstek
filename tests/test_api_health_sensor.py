"""The API degraded binary sensor mirrors the polling layer's health breaker."""

from types import SimpleNamespace

import pytest
from homeassistant.const import EntityCategory
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.marstek.const import (
    DOMAIN,
    PASSIVE_STATE_ACKNOWLEDGED,
    PASSIVE_STATE_RETRYING,
    PASSIVE_STATE_SENT,
    PASSIVE_STATE_UNKNOWN,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

ENTITY_ID = "binary_sensor.venus_a_battery_system_api_degraded"
API_METHODS = (
    "get_battery_status",
    "get_es_status",
    "get_es_mode",
    "get_em_status",
    "get_pv_status",
    "get_wifi_status",
    "get_ble_status",
)


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr("custom_components.marstek.monotonic", lambda: clock.now)
    return clock


@pytest.fixture
async def coordinator(hass, marstek_entry, mock_marstek_api, clock):
    marstek_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(marstek_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][marstek_entry.entry_id]
    yield coordinator
    await coordinator.async_stop_passive_control()


def degrade(policy, now):
    for key in ("battery", "pv", "wifi"):
        policy.record(key, False, now)
    assert policy.degraded


def recover(policy, now):
    for elapsed in (0, 30, 60):
        policy.record("es", True, now + elapsed)
    assert not policy.degraded


def sensor_state(hass):
    state = hass.states.get(ENTITY_ID)
    assert state is not None
    return state


async def test_initial_healthy_state_is_off(hass, coordinator):
    state = sensor_state(hass)
    assert state.state == "off"
    assert state.attributes["friendly_name"] == "Venus A Battery System API degraded"
    assert "device_class" not in state.attributes
    assert state.attributes["health_state"] == "healthy"
    assert state.attributes["failing_endpoints"] == []
    assert state.attributes["recent_failure_count"] == 0
    assert state.attributes["degraded_since"] is None


async def test_transitions_publish_immediately(hass, coordinator, clock):
    """No poll or coordinator update is needed to publish a transition."""
    degrade(coordinator._polling, clock.now)
    state = sensor_state(hass)
    assert state.state == "on"
    assert state.attributes["health_state"] == "degraded"
    assert state.attributes["failing_endpoints"] == ["battery", "pv", "wifi"]
    assert state.attributes["recent_failure_count"] == 3
    assert state.attributes["degraded_since"] is not None

    recover(coordinator._polling, clock.now + 1)
    state = sensor_state(hass)
    assert state.state == "off"
    assert state.attributes["health_state"] == "healthy"
    assert state.attributes["degraded_since"] is None


async def test_stays_available_through_complete_outage_and_recovers(
    hass, coordinator, mock_marstek_api, clock
):
    working = {
        method: getattr(mock_marstek_api, method).return_value
        for method in API_METHODS
    }
    for method in API_METHODS:
        getattr(mock_marstek_api, method).return_value = None
    clock.now += 300
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert sensor_state(hass).state == "on"

    # Once cached data runs out the coordinator fails; normal entities go
    # unavailable but the health sensor must still report the outage.
    coordinator.last_update_success = False
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.charging_allowed").state == "unavailable"
    assert sensor_state(hass).state == "on"

    for method, result in working.items():
        getattr(mock_marstek_api, method).return_value = result
    for _ in range(3):
        clock.now += 120
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert not coordinator._polling.degraded
    assert sensor_state(hass).state == "off"


async def test_entity_generates_no_api_requests(
    hass, coordinator, mock_marstek_api, clock
):
    mock_marstek_api.reset_mock()
    degrade(coordinator._polling, clock.now)
    recover(coordinator._polling, clock.now + 1)
    await hass.async_block_till_done()
    sensor_state(hass)
    assert mock_marstek_api.mock_calls == []


async def test_passive_acknowledgement_does_not_affect_health(
    hass, coordinator, mock_marstek_api, clock
):
    for passive_state in (
        PASSIVE_STATE_SENT,
        PASSIVE_STATE_RETRYING,
        PASSIVE_STATE_UNKNOWN,
        PASSIVE_STATE_ACKNOWLEDGED,
    ):
        coordinator._set_passive_power_state(passive_state)
        await hass.async_block_till_done()
        assert sensor_state(hass).state == "off"

    # A refused Passive command is not a read failure for the health breaker.
    mock_marstek_api.set_es_mode_passive.return_value = False
    assert not await coordinator.async_set_passive_power(240)
    assert sensor_state(hass).state == "off"

    # Nor does an accepted command clear an open breaker.
    degrade(coordinator._polling, clock.now)
    mock_marstek_api.set_es_mode_passive.return_value = True
    assert await coordinator.async_set_passive_power(300)
    assert sensor_state(hass).state == "on"


async def test_registered_as_diagnostic_on_existing_device(
    hass, coordinator, marstek_entry
):
    entry = er.async_get(hass).async_get(ENTITY_ID)
    assert entry.unique_id == f"{marstek_entry.unique_id}_api_degraded"
    assert entry.entity_category is EntityCategory.DIAGNOSTIC
    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, marstek_entry.unique_id)}
    )
    assert device is not None
    assert entry.device_id == device.id
    charging = er.async_get(hass).async_get("binary_sensor.charging_allowed")
    assert charging.device_id == device.id
