"""Exercise passive interruption recovery through real coordinator polling."""

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import call

import pytest

from custom_components.marstek import MarstekDataUpdateCoordinator
from custom_components.marstek.const import PASSIVE_SETTLE_SECONDS

pytestmark = pytest.mark.asyncio


def status(power, energy=12.25):
    return {
        "ongrid_power": power,
        "bat_soc": 50,
        "total_grid_input_energy": energy,
        "total_grid_output_energy": 3.5,
        "total_pv_energy": 1.0,
        "total_load_energy": 5.0,
    }


def mode(power):
    return {"mode": "Passive", "ongrid_power": power, "bat_soc": 50}


@pytest.fixture
async def recovery(hass, marstek_entry, mock_marstek_api, monkeypatch, caplog, request):
    """Start with an acknowledged, calibrated target."""
    desired = getattr(request, "param", -768)
    marstek_entry.add_to_hass(hass)
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr("custom_components.marstek.monotonic", lambda: clock.now)
    caplog.set_level(logging.DEBUG, logger="custom_components.marstek")
    coordinator = MarstekDataUpdateCoordinator(hass, mock_marstek_api, marstek_entry)
    mock_marstek_api.get_es_status.return_value = status(desired)
    mock_marstek_api.get_es_mode.return_value = mode(desired)
    mock_marstek_api.get_battery_status.return_value = {
        "soc": 50,
        "charg_flag": True,
        "dischrg_flag": True,
        "bat_temp": 27,
    }
    coordinator.calibration.observe(desired, desired, desired - 13)
    await coordinator.async_set_passive_power(desired)
    clock.now += PASSIVE_SETTLE_SECONDS + 1
    await coordinator.async_refresh()
    assert coordinator.passive_power_state == "acknowledged"

    async def settle():
        assert not coordinator._passive_command_lock.locked()
        clock.now += PASSIVE_SETTLE_SECONDS
        await asyncio.sleep(0)

    monkeypatch.setattr(coordinator, "_async_wait_passive_settle", settle)
    mock_marstek_api.reset_mock()
    caplog.clear()
    yield coordinator, mock_marstek_api, clock
    await coordinator.async_stop_passive_control()


def observations(caplog):
    prefix = "Marstek passive observation: "
    return [
        json.loads(record.getMessage()[len(prefix) :])
        for record in caplog.records
        if record.getMessage().startswith(prefix)
    ]


@pytest.mark.parametrize("recovery", [-760, 340], indirect=True)
async def test_confirmed_zero_recovers_compensated_target_and_publishes_new_data(
    recovery,
    caplog,
):
    coordinator, api, _ = recovery
    original_map = coordinator.calibration_snapshot()
    original_command = coordinator.command_power
    desired = coordinator.desired_power
    api.get_es_status.side_effect = [status(0), status(0), status(desired, 12.49)]
    api.get_es_mode.side_effect = [mode(0), mode(0), mode(desired)]
    published = []
    cancel = coordinator.async_add_listener(
        lambda: published.append(
            (coordinator.data["es"]["ongrid_power"], coordinator.passive_power_state)
        )
    )
    try:
        await coordinator.async_refresh()
    finally:
        cancel()
    api.set_es_mode_passive.assert_called_once_with(original_command)
    assert coordinator.desired_power == desired
    assert coordinator.calibration_snapshot() == original_map
    assert not coordinator._passive_samples
    assert published == [(desired, "acknowledged")]
    assert coordinator.data["es"]["total_grid_input_energy"] == 12.49
    events = observations(caplog)
    assert [event["phase"] for event in events] == [
        "suspected_interruption",
        "confirmation",
        "post_recovery",
    ]
    before = events[0]
    assert before["es_power_w"] == before["mode_power_w"] == 0
    assert before["battery_soc"] == before["es_soc"] == before["mode_soc"] == 50
    assert before["charg_flag"] is True
    assert before["energy_counters_raw"]["total_grid_input_energy"] == 12.25
    assert before["preceding_command"]["command_w"] == original_command
    assert before["preceding_command"]["success"] is True
    assert events[-1]["preceding_command"]["source"] == "verification_retry"
    assert api.get_es_status.call_count == api.get_es_mode.call_count == 3


@pytest.mark.parametrize("recovery", [-760, 340], indirect=True)
async def test_transient_zero_marks_unknown_before_confirmation_without_resending(
    recovery, monkeypatch, caplog
):
    coordinator, api, _ = recovery
    desired = coordinator.desired_power
    original_map = coordinator.calibration_snapshot()
    api.get_es_status.side_effect = [status(0), status(desired)]
    api.get_es_mode.side_effect = [mode(0), mode(desired)]
    read = coordinator._async_read_passive_telemetry

    async def confirm(data):
        assert coordinator.passive_power_state == "unknown"
        assert not coordinator._passive_samples
        api.set_es_mode_passive.assert_not_called()
        return await read(data)

    monkeypatch.setattr(coordinator, "_async_read_passive_telemetry", confirm)
    await coordinator.async_refresh()
    api.set_es_mode_passive.assert_not_called()
    assert coordinator.passive_power_state == "acknowledged"
    assert coordinator.calibration_snapshot() == original_map
    assert [power for _, power in coordinator._passive_samples] == [desired]
    assert api.get_es_status.call_count == api.get_es_mode.call_count == 2
    assert [event["phase"] for event in observations(caplog)] == [
        "suspected_interruption",
        "confirmation",
    ]


@pytest.mark.parametrize(
    "first_status,first_mode", [(0, -768), (-768, 0), (0, 0), (-768, -400)]
)
async def test_transient_zero_or_disagreement_is_confirmed_without_a_write(
    recovery,
    first_status,
    first_mode,
    caplog,
):
    coordinator, api, _ = recovery
    original_map = coordinator.calibration_snapshot()
    api.get_es_status.side_effect = [status(first_status), status(-768)]
    api.get_es_mode.side_effect = [mode(first_mode), mode(-768)]
    await coordinator.async_refresh()
    api.set_es_mode_passive.assert_not_called()
    assert coordinator.passive_power_state == "acknowledged"
    assert coordinator.data["es"]["ongrid_power"] == -768
    assert coordinator.calibration_snapshot() == original_map
    assert len(observations(caplog)) == 2


async def test_persistent_endpoint_disagreement_never_acknowledges_or_learns(recovery):
    coordinator, api, _ = recovery
    original_map = coordinator.calibration_snapshot()
    api.get_es_status.return_value = status(0)
    api.get_es_mode.return_value = mode(-768)
    await coordinator.async_refresh()
    api.set_es_mode_passive.assert_not_called()
    assert coordinator.passive_power_state == "unknown"
    assert not coordinator._passive_samples
    assert coordinator.calibration_snapshot() == original_map
    assert api.get_es_status.call_count == api.get_es_mode.call_count == 2


@pytest.mark.parametrize("missing", ["get_es_status", "get_es_mode"])
async def test_cached_endpoint_cannot_acknowledge_or_learn(recovery, missing):
    coordinator, api, _ = recovery
    getattr(api, missing).return_value = None
    await coordinator.async_refresh()
    assert coordinator.passive_power_state == "unknown"
    assert not coordinator._passive_samples
    api.set_es_mode_passive.assert_not_called()


@pytest.mark.parametrize("recovery", [-760, 340], indirect=True)
@pytest.mark.parametrize(
    "battery",
    [
        {"soc": 50, "charg_flag": False},
        {"soc": 50, "charg_flag": 0},
        {"soc": 100, "charg_flag": True},
        {"soc": 50},
        {"charg_flag": True},
        None,
    ],
)
async def test_zero_recovery_requires_fresh_charging_permission_only_for_charging(
    recovery, battery, caplog
):
    coordinator, api, _ = recovery
    api.get_es_status.return_value = {"ongrid_power": 0}
    api.get_es_mode.return_value = mode(0)
    api.get_battery_status.return_value = battery
    await coordinator.async_refresh()
    assert coordinator.passive_power_state == "unknown"
    assert not coordinator._passive_samples
    if coordinator.desired_power < 0:
        api.set_es_mode_passive.assert_not_called()
        assert coordinator._passive_keepalive_cancel is None
        assert coordinator._passive_charge_recovery_blocked
        assert observations(caplog)[-1]["phase"] == "recovery_blocked"
    else:
        api.set_es_mode_passive.assert_called_once_with(coordinator.command_power)
        assert coordinator._passive_keepalive_cancel is not None
        assert not coordinator._passive_charge_recovery_blocked
        events = observations(caplog)
        assert [event["phase"] for event in events] == [
            "suspected_interruption",
            "confirmation",
            "post_recovery",
        ]
        assert events[-1]["preceding_command"]["source"] == "verification_retry"


async def test_charging_permission_returning_allows_recovery(recovery):
    coordinator, api, _ = recovery
    api.get_es_status.return_value = status(0)
    api.get_es_mode.return_value = mode(0)
    api.get_battery_status.return_value = {"soc": 50, "charg_flag": False}
    await coordinator.async_refresh()
    api.get_battery_status.return_value = {"soc": 50, "charg_flag": True}
    api.get_es_status.side_effect = [status(0), status(0), status(-768)]
    api.get_es_mode.side_effect = [mode(0), mode(0), mode(-768)]
    await coordinator.async_refresh()
    api.set_es_mode_passive.assert_called_once_with(coordinator.command_power)
    assert coordinator.passive_power_state == "acknowledged"
    assert coordinator._passive_keepalive_cancel is not None


@pytest.mark.parametrize("recovery", [-760, 340], indirect=True)
@pytest.mark.parametrize("response", [0, None])
async def test_failed_or_zero_post_recovery_read_does_not_loop_or_reuse_cache(
    recovery, response
):
    coordinator, api, _ = recovery
    api.get_es_status.side_effect = [
        status(0),
        status(0),
        None if response is None else status(0),
    ]
    api.get_es_mode.side_effect = [
        mode(0),
        mode(0),
        None if response is None else mode(0),
    ]
    await coordinator.async_refresh()
    api.set_es_mode_passive.assert_called_once_with(coordinator.command_power)
    assert coordinator.passive_power_state == "unknown"
    assert not coordinator._passive_samples
    assert api.get_es_status.call_count == api.get_es_mode.call_count == 3
    if response is None:
        assert "es" not in coordinator.data
        assert "es_mode" not in coordinator.data
        assert "es" not in coordinator._last_good_data


async def test_new_target_during_settle_supersedes_recovery(recovery, monkeypatch):
    coordinator, api, clock = recovery
    maintained = coordinator.command_power
    api.get_es_status.side_effect = [status(0), status(0), status(-400)]
    api.get_es_mode.side_effect = [mode(0), mode(0), mode(-400)]

    async def settle():
        assert not coordinator._passive_command_lock.locked()
        await coordinator.async_set_passive_power(-400)
        clock.now += PASSIVE_SETTLE_SECONDS

    monkeypatch.setattr(coordinator, "_async_wait_passive_settle", settle)
    await coordinator.async_refresh()
    assert coordinator.desired_power == -400
    assert coordinator.passive_power_state == "acknowledged"
    assert api.set_es_mode_passive.call_args_list == [
        call(maintained),
        call(coordinator.command_power),
    ]
    assert coordinator.data["es"]["ongrid_power"] == -400


async def test_permission_lost_after_recovery_pauses_maintenance(recovery, caplog):
    coordinator, api, _ = recovery
    api.get_es_status.return_value = status(0)
    api.get_es_mode.return_value = mode(0)
    api.get_battery_status.side_effect = [
        {"soc": 50, "charg_flag": True},
        {"soc": 50, "charg_flag": True},
        {"soc": 50, "charg_flag": False},
    ]
    await coordinator.async_refresh()
    api.set_es_mode_passive.assert_called_once_with(coordinator.command_power)
    assert coordinator._passive_keepalive_cancel is None
    assert coordinator.passive_power_state == "unknown"
    assert "recovery_blocked" in [event["phase"] for event in observations(caplog)]


async def test_command_changed_during_confirmation_invalidates_power_snapshot(
    recovery, monkeypatch
):
    coordinator, api, clock = recovery
    api.get_es_status.return_value = status(0)
    api.get_es_mode.return_value = mode(0)
    read = coordinator._async_read_passive_telemetry

    async def read_then_change(data):
        result = await read(data)
        await coordinator.async_set_passive_power(-400)
        clock.now += PASSIVE_SETTLE_SECONDS
        return result

    monkeypatch.setattr(coordinator, "_async_read_passive_telemetry", read_then_change)
    await coordinator.async_refresh()
    api.set_es_mode_passive.assert_called_once_with(coordinator.command_power)
    assert coordinator.desired_power == -400
    assert coordinator.passive_power_state == "sent"
    assert "es" not in coordinator.data
    assert "es_mode" not in coordinator.data
    assert not coordinator._passive_samples


async def test_stop_during_settle_cannot_be_undone_by_recovery(recovery, monkeypatch):
    coordinator, api, clock = recovery
    maintained = coordinator.command_power
    api.get_es_status.side_effect = [status(0), status(0), status(0)]
    api.get_es_mode.side_effect = [
        mode(0),
        mode(0),
        {"mode": "Auto", "ongrid_power": 0},
    ]

    async def stop_while_settling():
        await coordinator.async_set_operating_mode("Auto")
        clock.now += PASSIVE_SETTLE_SECONDS

    monkeypatch.setattr(coordinator, "_async_wait_passive_settle", stop_while_settling)
    await coordinator.async_refresh()
    api.set_es_mode_passive.assert_called_once_with(maintained)
    api.set_es_mode_auto.assert_called_once_with()
    assert coordinator.desired_power is None
    assert coordinator._passive_keepalive_cancel is None
    assert coordinator.data["es_mode"]["mode"] == "Auto"


async def test_pending_new_command_gets_settling_time_before_zero_confirmation(
    recovery, monkeypatch
):
    coordinator, api, clock = recovery
    await coordinator.async_set_passive_power(-768)
    api.reset_mock()
    api.get_es_status.side_effect = [status(0), status(-768)]
    api.get_es_mode.side_effect = [mode(0), mode(-768)]
    waits = []

    async def settle():
        waits.append(clock.now)
        clock.now += PASSIVE_SETTLE_SECONDS

    monkeypatch.setattr(coordinator, "_async_wait_passive_settle", settle)
    await coordinator.async_refresh()
    assert len(waits) == 1
    api.set_es_mode_passive.assert_not_called()
    assert coordinator.passive_power_state == "acknowledged"


async def test_failed_recovery_write_is_followed_by_measurement_without_an_inline_retry(
    recovery,
):
    coordinator, api, _ = recovery
    api.get_es_status.return_value = status(0)
    api.get_es_mode.return_value = mode(0)
    api.set_es_mode_passive.return_value = False
    await coordinator.async_refresh()
    api.set_es_mode_passive.assert_called_once_with(coordinator.command_power)
    assert api.get_es_status.call_count == api.get_es_mode.call_count == 3
    assert coordinator.passive_power_state == "unknown"
    assert coordinator._passive_keepalive_cancel is not None
