"""Endpoint deadlines, health hysteresis and command admission."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.marstek import MarstekDataUpdateCoordinator
from custom_components.marstek.polling import (
    ENDPOINTS,
    CommandPriorityGate,
    PollingPolicy,
)

METHODS = {
    "es": "get_es_status",
    "es_mode": "get_es_mode",
    "battery": "get_battery_status",
    "em": "get_em_status",
    "pv": "get_pv_status",
    "wifi": "get_wifi_status",
    "ble": "get_ble_status",
}


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr("custom_components.marstek.monotonic", lambda: clock.now)
    return clock


@pytest.fixture
async def coordinator(hass, marstek_entry, mock_marstek_api, clock):
    marstek_entry.add_to_hass(hass)
    coordinator = MarstekDataUpdateCoordinator(hass, mock_marstek_api, marstek_entry)
    yield coordinator
    await coordinator.async_stop_passive_control()


@pytest.mark.parametrize("key", ENDPOINTS)
async def test_endpoint_intervals_and_cached_skips(coordinator, clock, key):
    fetch = Mock(return_value={"value": 42})
    interval = ENDPOINTS[key][1]
    assert await coordinator._fetch_section(key, fetch) == {"value": 42}
    for elapsed in (0, interval / 2, interval - 0.001):
        clock.now = 1000 + elapsed
        assert await coordinator._fetch_section(key, fetch) == {"value": 42}
        assert key not in coordinator._fresh_sections
        assert coordinator._missing_cycles[key] == 0
        assert fetch.call_count == 1
    clock.now = 1000 + interval
    await coordinator._fetch_section(key, fetch)
    assert fetch.call_count == 2
    assert key in coordinator._fresh_sections


@pytest.mark.parametrize("key", set(ENDPOINTS) - {"ble"})
async def test_failures_back_off_only_failed_endpoint_and_reset(
    coordinator, clock, key
):
    fetch = Mock(return_value=None)
    state = coordinator._polling.sections[key]
    for failure in range(1, 5):
        now = clock.now
        assert await coordinator._fetch_section(key, fetch) is None
        delay = min(state.cap, state.interval * 2**failure)
        assert state.next_poll == now + delay
        assert state.consecutive_failures == failure
        clock.now = state.next_poll - 0.001
        await coordinator._fetch_section(key, fetch)
        assert fetch.call_count == failure
        assert not coordinator._polling.degraded
        assert all(
            s.consecutive_failures == 0
            for k, s in coordinator._polling.sections.items()
            if k != key
        )
        clock.now = state.next_poll
    fetch.return_value = {}
    assert await coordinator._fetch_section(key, fetch) == {}
    assert state.consecutive_failures == 0
    assert state.last_success == clock.now
    assert state.next_poll == clock.now + state.interval


def test_configurable_caps_and_completion_deadlines():
    policy = PollingPolicy("test", intervals={"es": 10}, backoff_caps={"es": 25})
    policy.record("es", False, 1010)
    assert policy.sections["es"].next_poll == 1030
    policy.record("es", False, 1035)
    assert policy.sections["es"].next_poll == 1060
    with pytest.raises(ValueError):
        PollingPolicy("test", intervals={"es": 50}, backoff_caps={"es": 20})


def degrade(policy, now):
    for key in ("battery", "pv", "wifi"):
        policy.record(key, False, now)
    assert policy.degraded


def test_health_window_hysteresis_and_device_isolation(caplog):
    caplog.set_level("DEBUG")
    policy, other = PollingPolicy("one"), PollingPolicy("two")
    policy.record("battery", False, 0)
    policy.record("pv", False, 121)
    policy.record("wifi", False, 122)
    assert not policy.degraded
    policy.record("es", False, 123)
    assert policy.degraded
    assert not other.degraded
    for now in (124, 125, 126):
        policy.communication("ES.SetMode", True, now)
    assert policy.degraded
    policy.communication("es", False, 180)
    for now in (181, 210, 241):
        policy.communication("es", True, now)
    assert not policy.degraded
    policy.record("es", False, 242)
    assert not policy.degraded
    assert "healthy -> degraded" in caplog.text
    assert "degraded -> healthy" in caplog.text


async def test_degraded_polling_preserves_cache_and_recovers_by_probes(
    coordinator, mock_marstek_api, clock
):
    original = await coordinator._async_update_data()
    degrade(coordinator._polling, clock.now)
    mock_marstek_api.reset_mock()
    for elapsed in (30, 60, 90):
        clock.now = 1000 + elapsed
        assert await coordinator._async_update_data() == original
        assert not any(
            getattr(mock_marstek_api, method).called for method in METHODS.values()
        )
    for elapsed in (120, 240, 360):
        clock.now = 1000 + elapsed
        await coordinator._async_update_data()
    assert not coordinator._polling.degraded
    assert mock_marstek_api.get_es_status.call_count == 3
    assert mock_marstek_api.get_wifi_status.call_count == 0
    assert mock_marstek_api.get_battery_status.call_count == 0


async def test_degraded_commands_duplicates_retry_and_keepalive(
    coordinator, mock_marstek_api, clock
):
    degrade(coordinator._polling, clock.now)
    mock_marstek_api.set_es_mode_passive.return_value = False
    assert not await coordinator.async_set_passive_power(250)
    retry = coordinator._passive_retry
    assert retry is not None
    assert not await coordinator.async_set_passive_power(250)
    assert coordinator._passive_retry is retry
    retry.cancel()
    mock_marstek_api.set_es_mode_passive.return_value = True
    clock.now += 15
    await coordinator._async_retry_passive_power(retry)
    assert mock_marstek_api.set_es_mode_passive.call_count == 2
    assert coordinator._passive_keepalive_cancel is not None
    assert await coordinator.async_set_passive_power(250)
    assert mock_marstek_api.set_es_mode_passive.call_count == 2
    coordinator._passive_keepalive_cancel()
    await coordinator._async_keepalive_passive_power(
        coordinator._passive_control_generation, source="keepalive"
    )
    assert mock_marstek_api.set_es_mode_passive.call_count == 3
    assert await coordinator.async_set_operating_mode("Auto")
    mock_marstek_api.set_es_mode_auto.assert_called_once()


async def test_priority_gate_serializes_and_commands_overtake_waiting_reads():
    gate = CommandPriorityGate()
    active, release = asyncio.Event(), asyncio.Event()
    order = []

    async def operation(name, command=False):
        async with gate.slot(command=command):
            order.append(name)
            if name == "active read":
                active.set()
                await release.wait()

    first = asyncio.create_task(operation("active read"))
    await active.wait()
    poll = asyncio.create_task(operation("waiting read"))
    await asyncio.sleep(0)
    command = asyncio.create_task(operation("command", True))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, poll, command)
    assert order == ["active read", "command", "waiting read"]


async def test_cancelled_waiting_command_does_not_starve_reads():
    gate = CommandPriorityGate()
    async with gate.slot():

        async def command():
            async with gate.slot(command=True):
                pytest.fail("Cancelled command was admitted")

        pending = asyncio.create_task(command())
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    async with gate.slot():
        assert gate.pending_commands == 0


async def test_healthy_passive_mode_reduction_and_unknown_verification(
    coordinator, mock_marstek_api, clock
):
    mock_marstek_api.get_es_status.return_value = {"ongrid_power": 250}
    mock_marstek_api.get_es_mode.return_value = {"mode": "Passive", "ongrid_power": 250}
    await coordinator.async_set_passive_power(250)
    await coordinator.async_refresh()
    assert coordinator.passive_power_state == "acknowledged"
    for elapsed in range(30, 300, 30):
        clock.now = 1000 + elapsed
        await coordinator.async_refresh()
        assert coordinator.passive_power_state == "acknowledged"
        assert mock_marstek_api.get_es_mode.call_count == 1
    clock.now = 1300
    await coordinator.async_refresh()
    assert mock_marstek_api.get_es_mode.call_count == 2
    clock.now = 1330
    coordinator._set_passive_power_state("unknown")
    await coordinator.async_refresh()
    assert mock_marstek_api.get_es_mode.call_count == 3
    assert coordinator.passive_power_state == "acknowledged"


async def test_cached_mode_cannot_acknowledge_new_command(
    coordinator, mock_marstek_api, clock
):
    mock_marstek_api.get_es_status.return_value = {"ongrid_power": 250}
    mock_marstek_api.get_es_mode.return_value = {"mode": "Passive", "ongrid_power": 250}
    await coordinator.async_set_passive_power(250)
    await coordinator.async_refresh()
    assert coordinator.passive_power_state == "acknowledged"
    mock_marstek_api.get_es_mode.return_value = None
    clock.now += 30
    await coordinator.async_set_passive_power(260)
    await coordinator.async_refresh()
    assert coordinator.passive_power_state == "unknown"
    assert coordinator._polling.sections["es_mode"].consecutive_failures == 1
    assert mock_marstek_api.set_es_mode_passive.call_count == 2
    clock.now += 30
    await coordinator.async_refresh()
    assert mock_marstek_api.get_es_mode.call_count == 2


async def test_empty_startup_still_fails_and_intentional_skips_keep_availability(
    coordinator, mock_marstek_api, clock
):
    for method in METHODS.values():
        getattr(mock_marstek_api, method).return_value = None
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    coordinator._polling = PollingPolicy("test")
    mock_marstek_api.get_es_status.return_value = {"ongrid_power": 10}
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    clock.now += 1
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.data["es"] == {"ongrid_power": 10}


@pytest.mark.parametrize("cancel_poll", [False, True])
async def test_coordinator_prioritizes_all_queued_commands_and_drains_cancelled_io(
    coordinator, hass, mock_marstek_api, monkeypatch, cancel_poll
):
    """Queued control producers win; cancellation cannot release active I/O."""
    started, finish = asyncio.Event(), asyncio.Event()
    order = []
    active = 0

    async def dispatch(target, *args):
        nonlocal active
        assert active == 0
        active += 1
        try:
            if target == mock_marstek_api.get_battery_status:
                order.append("active poll")
                started.set()
                await finish.wait()
                return {"soc": 50}
            if target == mock_marstek_api.set_es_mode_passive:
                order.append(args[0])
                await asyncio.sleep(0)
                return True
            order.append("queued poll")
            return {}
        finally:
            active -= 1

    monkeypatch.setattr(hass, "async_add_executor_job", dispatch)
    first = asyncio.create_task(
        coordinator._fetch_section("battery", mock_marstek_api.get_battery_status)
    )
    await asyncio.wait_for(started.wait(), 1)
    queued = asyncio.create_task(
        coordinator._fetch_section("wifi", mock_marstek_api.get_wifi_status)
    )
    command = asyncio.create_task(coordinator.async_set_passive_power(240))
    await asyncio.sleep(0)
    second_command = asyncio.create_task(coordinator.async_set_passive_power(300))
    await asyncio.sleep(0)
    assert coordinator._io_gate.pending_controls == 2
    if cancel_poll:
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()  # Even repeated cancellation must drain the worker.
        await asyncio.sleep(0)
    assert order == ["active poll"]
    finish.set()
    results = await asyncio.wait_for(
        asyncio.gather(first, queued, command, second_command, return_exceptions=True),
        2,
    )
    assert order == ["active poll", 240, 300, "queued poll"]
    assert (
        isinstance(results[0], asyncio.CancelledError)
        if cancel_poll
        else results[0] == {"soc": 50}
    )
    assert results[1:] == [{}, True, True]
    assert coordinator._io_gate.pending_controls == 0


async def test_failed_reads_during_degradation_do_not_trigger_recovery_traffic(
    coordinator, mock_marstek_api, clock
):
    mock_marstek_api.get_es_status.return_value = {"ongrid_power": 250}
    mock_marstek_api.get_es_mode.return_value = {"mode": "Passive", "ongrid_power": 250}
    await coordinator.async_set_passive_power(250)
    await coordinator.async_refresh()
    degrade(coordinator._polling, clock.now)
    mock_marstek_api.reset_mock()
    mock_marstek_api.get_es_status.return_value = None
    for elapsed in range(30, 301, 30):
        clock.now = 1000 + elapsed
        await coordinator.async_refresh()
    assert coordinator.passive_power_state == "unknown"
    assert coordinator._passive_retry is None
    mock_marstek_api.set_es_mode_passive.assert_not_called()
    # ES probes obey both health and endpoint backoff. Mode verification is
    # requested on the state/freshness transition, not repeatedly while stale.
    assert mock_marstek_api.get_es_status.call_count == 2
    assert mock_marstek_api.get_es_mode.call_count <= 2
    for key in ("wifi", "battery", "pv"):
        getattr(mock_marstek_api, METHODS[key]).assert_not_called()
        assert key in coordinator.data


@pytest.mark.parametrize(
    "reason", ["stale", "failed_status", "failed_mode", "failed_command", "recovery"]
)
async def test_stable_passive_leaves_slow_mode_polling_when_verification_needed(
    coordinator, mock_marstek_api, clock, reason
):
    mock_marstek_api.get_es_status.return_value = {"ongrid_power": 250}
    mock_marstek_api.get_es_mode.return_value = {"mode": "Passive", "ongrid_power": 250}
    await coordinator.async_set_passive_power(250)
    await coordinator.async_refresh()
    clock.now += 30
    await coordinator.async_refresh()
    assert coordinator._polling.sections["es_mode"].interval == 300
    assert mock_marstek_api.get_es_mode.call_count == 1
    if reason == "stale":
        clock.now += 61
    elif reason == "failed_status":
        clock.now += 30
        mock_marstek_api.get_es_status.return_value = None
    elif reason == "failed_mode":
        clock.now = 1300
        mock_marstek_api.get_es_mode.return_value = None
    elif reason == "failed_command":
        mock_marstek_api.set_es_mode_passive.return_value = False
        await coordinator.async_set_passive_power(260)
    else:
        coordinator._schedule_passive_retry("verification_retry")
    await coordinator.async_refresh()
    assert mock_marstek_api.get_es_mode.call_count == 2
    if reason in {"failed_status", "failed_mode"}:
        assert coordinator.passive_power_state == "unknown"


async def test_degraded_new_command_can_be_verified_without_nonessential_reads(
    coordinator, mock_marstek_api, clock
):
    await coordinator.async_refresh()
    degrade(coordinator._polling, clock.now)
    mock_marstek_api.get_es_status.return_value = {"ongrid_power": 250}
    mock_marstek_api.get_es_mode.return_value = {"mode": "Passive", "ongrid_power": 250}
    assert await coordinator.async_set_passive_power(250)
    mock_marstek_api.reset_mock()
    clock.now += 120
    await coordinator.async_refresh()
    assert coordinator.passive_power_state == "acknowledged"
    assert mock_marstek_api.get_es_status.call_count == 1
    assert mock_marstek_api.get_es_mode.call_count == 1
    mock_marstek_api.get_battery_status.assert_not_called()
    mock_marstek_api.get_wifi_status.assert_not_called()


async def test_pv_recovers_without_reload_and_keeps_cached_entity_value(
    coordinator, mock_marstek_api, clock
):
    mock_marstek_api.get_pv_status.return_value = {"pv1_power": 42}
    await coordinator.async_refresh()
    clock.now += 60
    mock_marstek_api.get_pv_status.return_value = None
    await coordinator.async_refresh()
    assert coordinator.data["pv"] == {"pv1_power": 42}
    assert "pv" not in coordinator._disabled_optional_sections
    clock.now += 30
    await coordinator.async_refresh()
    assert mock_marstek_api.get_pv_status.call_count == 2
    assert coordinator.data["pv"] == {"pv1_power": 42}
    clock.now += 90
    mock_marstek_api.get_pv_status.return_value = {"pv1_power": 100}
    await coordinator.async_refresh()
    assert coordinator.data["pv"] == {"pv1_power": 100}
    assert coordinator._polling.sections["pv"].consecutive_failures == 0


async def test_raised_timeout_uses_endpoint_backoff(coordinator, clock):
    fetch = Mock(side_effect=TimeoutError)
    assert await coordinator._fetch_section("es", fetch) is None
    assert coordinator._polling.sections["es"].next_poll == clock.now + 60
    assert coordinator._missing_cycles["es"] == 1


@pytest.mark.parametrize("degraded", [False, True])
async def test_zero_at_normal_cadence_recovers_even_when_mode_poll_was_skipped(
    coordinator, mock_marstek_api, clock, monkeypatch, degraded
):
    """A fresh ES interruption can start bounded verification between mode polls."""
    mock_marstek_api.get_es_status.return_value = {"ongrid_power": 250}
    mock_marstek_api.get_es_mode.return_value = {"mode": "Passive", "ongrid_power": 250}
    await coordinator.async_set_passive_power(250)
    await coordinator.async_refresh()
    mock_marstek_api.reset_mock()
    if degraded:
        degrade(coordinator._polling, clock.now)
    clock.now += 120 if degraded else 30
    mock_marstek_api.get_es_status.side_effect = [
        {"ongrid_power": 0},
        {"ongrid_power": 0},
        {"ongrid_power": 250},
    ]
    mock_marstek_api.get_es_mode.side_effect = [
        {"mode": "Passive", "ongrid_power": 0},
        {"mode": "Passive", "ongrid_power": 250},
    ]

    if degraded:
        # The longer probe gap also makes ES stale, so mode verification runs
        # in the initial round as well as confirmation and post-command reads.
        mock_marstek_api.get_es_mode.side_effect = [
            {"mode": "Passive", "ongrid_power": 0},
            {"mode": "Passive", "ongrid_power": 0},
            {"mode": "Passive", "ongrid_power": 250},
        ]

    async def settle():
        clock.now += 15

    monkeypatch.setattr(coordinator, "_async_wait_passive_settle", settle)
    await coordinator.async_refresh()
    mock_marstek_api.set_es_mode_passive.assert_called_once_with(250)
    assert mock_marstek_api.get_es_status.call_count == 3
    assert mock_marstek_api.get_es_mode.call_count == (3 if degraded else 2)
    assert coordinator.passive_power_state == "acknowledged"
    assert coordinator.data["es"]["ongrid_power"] == 250


async def test_mode_failure_backoff_survives_unknown_verification_request(
    coordinator, mock_marstek_api, clock
):
    mock_marstek_api.get_es_status.return_value = {"ongrid_power": 250}
    mock_marstek_api.get_es_mode.return_value = {"mode": "Passive", "ongrid_power": 250}
    await coordinator.async_set_passive_power(250)
    await coordinator.async_refresh()
    clock.now += 30
    await coordinator.async_refresh()
    for now in range(1060, 1300, 30):
        clock.now = now
        await coordinator.async_refresh()
    clock.now = 1300
    mock_marstek_api.get_es_mode.return_value = None
    await coordinator.async_refresh()
    assert coordinator.passive_power_state == "unknown"
    deadline = coordinator._polling.sections["es_mode"].next_poll
    for elapsed in range(30, 300, 30):
        clock.now = 1300 + elapsed
        await coordinator.async_refresh()
        assert mock_marstek_api.get_es_mode.call_count == 2
        assert coordinator._polling.sections["es_mode"].next_poll == deadline
    clock.now = deadline
    mock_marstek_api.get_es_mode.return_value = {"mode": "Passive", "ongrid_power": 250}
    await coordinator.async_refresh()
    assert coordinator.passive_power_state == "acknowledged"
    assert coordinator._polling.sections["es_mode"].consecutive_failures == 0
