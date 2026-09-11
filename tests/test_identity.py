"""Regression tests for device identity across setup, recovery and reloads."""

import asyncio
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.marstek.const import DOMAIN
from custom_components.marstek.identity import CONF_DEVICE_INFO

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.usefixtures("enable_custom_integrations"),
]


def registry_snapshot(hass, entry):
    """Record the identities visible to users, including all four platforms."""
    return {
        entity.entity_id: (entity.unique_id, entity.device_id)
        for entity in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }


async def test_restart_with_missing_metadata_keeps_all_entities(
    hass,
    marstek_entry,
    mock_marstek_api,
):
    """A successful setup followed by timeouts must not create a second device."""
    marstek_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(marstek_entry.entry_id)
    await hass.async_block_till_done()
    original = registry_snapshot(hass, marstek_entry)
    assert {entity_id.split(".")[0] for entity_id in original} == {
        "sensor",
        "binary_sensor",
        "select",
        "number",
    }
    assert len({device_id for _, device_id in original.values()}) == 1
    assert marstek_entry.data[CONF_DEVICE_INFO]["device"] == "Venus A"

    for response in (None, {}, {"device": "Venus A"}):
        mock_marstek_api.get_device_info.return_value = response
        assert await hass.config_entries.async_reload(marstek_entry.entry_id)
        await hass.async_block_till_done()
        assert registry_snapshot(hass, marstek_entry) == original
        devices = dr.async_entries_for_config_entry(
            dr.async_get(hass), marstek_entry.entry_id
        )
        assert len(devices) == 1
        assert devices[0].name == "Venus A Battery System"
        assert devices[0].identifiers == {(DOMAIN, marstek_entry.unique_id)}


@pytest.mark.parametrize("initial_info", [None, {"ble_mac": "AA:BB:CC:DD:EE:01"}])
async def test_metadata_recovers_after_startup(
    hass, marstek_entry, mock_marstek_api, initial_info
):
    """Recovery updates display metadata in place and persists it for restart."""
    marstek_entry.add_to_hass(hass)
    info = mock_marstek_api.get_device_info.return_value
    mock_marstek_api.get_device_info.return_value = initial_info
    assert await hass.config_entries.async_setup(marstek_entry.entry_id)
    await hass.async_block_till_done()
    before = registry_snapshot(hass, marstek_entry)

    mock_marstek_api.get_device_info.return_value = info
    await hass.data[DOMAIN][marstek_entry.entry_id].async_refresh()
    await hass.async_block_till_done()
    assert registry_snapshot(hass, marstek_entry) == before
    device = dr.async_entries_for_config_entry(
        dr.async_get(hass), marstek_entry.entry_id
    )[0]
    assert device.name == "Venus A Battery System"
    assert device.sw_version == "123"
    assert marstek_entry.data[CONF_DEVICE_INFO] == info


async def test_legacy_entry_recovers_registry_metadata(
    hass,
    marstek_entry,
    mock_marstek_api,
    device_registry,
):
    """Old host/port-only entries retain the real device name during a timeout."""
    marstek_entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=marstek_entry.entry_id,
        identifiers={(DOMAIN, marstek_entry.unique_id)},
        name="Venus A Battery System",
        model="Venus A",
        sw_version="99",
    )
    mock_marstek_api.get_device_info.return_value = None
    assert await hass.config_entries.async_setup(marstek_entry.entry_id)
    await hass.async_block_till_done()
    updated = device_registry.async_get(device.id)
    assert updated.name == device.name
    assert updated.sw_version == "99"


@pytest.mark.parametrize("identity", [None, "", "unknown", "00:00:00:00:00:00"])
async def test_missing_identity_defers_setup_and_recovers(
    hass,
    marstek_entry,
    mock_marstek_api,
    identity,
    freezer,
):
    """Telemetry alone must never permit placeholder entity registration."""
    marstek_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(marstek_entry, unique_id=identity)
    info = mock_marstek_api.get_device_info.return_value
    mock_marstek_api.get_device_info.return_value = None
    assert not await hass.config_entries.async_setup(marstek_entry.entry_id)
    await hass.async_block_till_done()
    assert marstek_entry.state is ConfigEntryState.SETUP_RETRY
    assert not registry_snapshot(hass, marstek_entry)
    assert not dr.async_entries_for_config_entry(
        dr.async_get(hass), marstek_entry.entry_id
    )

    mock_marstek_api.get_device_info.return_value = info
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert marstek_entry.state is ConfigEntryState.LOADED
    assert marstek_entry.unique_id == info["ble_mac"].lower()
    assert registry_snapshot(hass, marstek_entry)


async def test_changed_mac_format_keeps_legacy_ids(
    hass, marstek_entry, mock_marstek_api
):
    """Firmware may change case/separators without changing physical identity."""
    marstek_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(marstek_entry.entry_id)
    await hass.async_block_till_done()
    original = registry_snapshot(hass, marstek_entry)
    mock_marstek_api.get_device_info.return_value = {
        "device": "Venus A",
        "ble_mac": "aabbccddee01",
        "ver": 124,
    }
    assert await hass.config_entries.async_reload(marstek_entry.entry_id)
    await hass.async_block_till_done()
    assert registry_snapshot(hass, marstek_entry) == original
    assert marstek_entry.unique_id == "AA:BB:CC:DD:EE:01"


async def test_wrong_battery_rejects_setup(hass, marstek_entry, mock_marstek_api):
    """An IP now occupied by another battery must not change the saved identity."""
    marstek_entry.add_to_hass(hass)
    mock_marstek_api.get_device_info.return_value = {
        "device": "Venus A",
        "ble_mac": "AA:BB:CC:DD:EE:02",
    }
    assert not await hass.config_entries.async_setup(marstek_entry.entry_id)
    assert marstek_entry.state is ConfigEntryState.SETUP_RETRY
    assert marstek_entry.unique_id == "AA:BB:CC:DD:EE:01"
    assert not registry_snapshot(hass, marstek_entry)


async def test_late_identity_mismatch_stops_commands(
    hass, marstek_entry, mock_marstek_api
):
    """A mismatch discovered after a timeout cancels ongoing battery control."""
    marstek_entry.add_to_hass(hass)
    mock_marstek_api.get_device_info.return_value = None
    assert await hass.config_entries.async_setup(marstek_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][marstek_entry.entry_id]
    assert await coordinator.async_set_passive_power(300)
    mock_marstek_api.set_es_mode_passive.reset_mock()
    mock_marstek_api.get_device_info.return_value = {"ble_mac": "AA:BB:CC:DD:EE:02"}
    await coordinator.async_refresh()
    assert not await coordinator.async_set_passive_power(500)
    assert not await coordinator.async_set_operating_mode("Auto")
    mock_marstek_api.set_es_mode_passive.assert_not_called()
    mock_marstek_api.set_es_mode_auto.assert_not_called()
    assert coordinator._passive_keepalive_cancel is None

    mock_marstek_api.get_device_info.return_value = None
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    mock_marstek_api.get_device_info.return_value = {"ble_mac": marstek_entry.unique_id}
    await coordinator.async_refresh()
    assert coordinator.last_update_success


async def test_two_identical_models_keep_separate_entities_and_commands(
    hass,
    marstek_entry,
    mock_marstek_api,
):
    """Two batteries remain isolated when either is reloaded without metadata."""
    second = MockConfigEntry(
        domain=DOMAIN,
        title="Marstek Venus A",
        unique_id="AA:BB:CC:DD:EE:02",
        data={"host": "192.0.2.2", "port": 30000},
    )
    marstek_entry.add_to_hass(hass)
    second_api = MagicMock()
    for method in (
        "get_device_info",
        "get_wifi_status",
        "get_ble_status",
        "get_pv_status",
        "get_es_status",
        "get_em_status",
    ):
        getattr(second_api, method).return_value = None
    second_api.get_battery_status.return_value = {"soc": 80}
    second_api.get_es_mode.return_value = {"mode": "Auto"}
    second_api.set_es_mode_passive.return_value = True

    def api_for_host(*, host, port):
        return mock_marstek_api if host == "192.0.2.1" else second_api

    with patch("custom_components.marstek.MarstekAPI", side_effect=api_for_host):
        assert await hass.config_entries.async_setup(marstek_entry.entry_id)
        await hass.async_block_till_done()
        second.add_to_hass(hass)
        assert await hass.config_entries.async_setup(second.entry_id)
        await hass.async_block_till_done()
        first_snapshot = registry_snapshot(hass, marstek_entry)
        second_snapshot = registry_snapshot(hass, second)
        assert len(first_snapshot) == len(second_snapshot) > 0
        assert not set(first_snapshot.values()) & set(second_snapshot.values())
        assert len(dr.async_get(hass).devices) == 2
        mock_marstek_api.get_device_info.return_value = None
        assert await hass.config_entries.async_reload(marstek_entry.entry_id)
        await hass.async_block_till_done()
        assert registry_snapshot(hass, marstek_entry) == first_snapshot
        assert registry_snapshot(hass, second) == second_snapshot

        for entry, power, api, other_api in (
            (marstek_entry, 400, mock_marstek_api, second_api),
            (second, -700, second_api, mock_marstek_api),
        ):
            api.set_es_mode_passive.reset_mock()
            other_api.set_es_mode_passive.reset_mock()
            entity_id = er.async_get(hass).async_get_entity_id(
                "select",
                DOMAIN,
                f"{entry.unique_id}_operating_mode",
            )
            await hass.services.async_call(
                DOMAIN,
                "set_operating_mode_passive",
                {"entity_id": entity_id, "power": power, "cd_time": 3600},
                blocking=True,
            )
            api.set_es_mode_passive.assert_called_once_with(power)
            other_api.set_es_mode_passive.assert_not_called()


async def test_identity_mismatch_cancels_in_flight_command_keepalive(
    hass,
    marstek_entry,
    mock_marstek_api,
):
    """A completing command cannot restore its timer after an identity mismatch."""
    marstek_entry.add_to_hass(hass)
    mock_marstek_api.get_device_info.return_value = None
    assert await hass.config_entries.async_setup(marstek_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][marstek_entry.entry_id]
    command_started = asyncio.Event()
    finish_command = asyncio.Event()
    identity_received = asyncio.Event()
    executor = hass.async_add_executor_job

    async def dispatch(target, *args):
        if target == mock_marstek_api.set_es_mode_passive:
            command_started.set()
            await finish_command.wait()
            return True
        if target == mock_marstek_api.get_device_info:
            identity_received.set()
            return {"ble_mac": "AA:BB:CC:DD:EE:02"}
        return await executor(target, *args)

    with patch.object(hass, "async_add_executor_job", side_effect=dispatch):
        command = asyncio.create_task(coordinator.async_set_passive_power(300))
        await command_started.wait()
        refresh = asyncio.create_task(coordinator.async_refresh())
        try:
            await identity_received.wait()
            assert not refresh.done()
        finally:
            finish_command.set()
            await asyncio.gather(command, refresh)
    assert coordinator._passive_keepalive_cancel is None
    assert coordinator.passive_power_state == "unknown"
    assert not coordinator.last_update_success


async def test_legacy_entry_cannot_adopt_another_configured_battery(
    hass,
    marstek_entry,
    mock_marstek_api,
):
    """Recovering a missing identity must not claim an existing config entry's battery."""
    marstek_entry.add_to_hass(hass)
    legacy = MockConfigEntry(domain=DOMAIN, data={"host": "192.0.2.2"})
    legacy.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(legacy.entry_id)
    assert not registry_snapshot(hass, legacy)
