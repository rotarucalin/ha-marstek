"""Endpoint deadlines, health hysteresis and command admission."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.marstek import MarstekDataUpdateCoordinator
from custom_components.marstek.polling import (
    ACTIVE_INTERVALS,
    ACTIVE_PROFILE_MAX_SECONDS,
    ENDPOINTS,
    ES_FRESH_SECONDS,
    HEALTH_PROBE_INTERVAL,
    PROFILE_ACTIVE,
    PROFILE_NORMAL,
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
        assert coordinator._polling.profile == PROFILE_NORMAL
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


async def test_unknown_output_waits_for_normal_passive_polling(
    coordinator, mock_marstek_api, clock
):
    mock_marstek_api.get_es_status.return_value = {"ongrid_power": 250}
    mock_marstek_api.get_es_mode.return_value = {"mode": "Passive", "ongrid_power": 250}
    await coordinator.async_set_passive_power(250)
    await coordinator.async_refresh()
    assert coordinator.passive_power_state == "acknowledged"
    mode_interval = ENDPOINTS["es_mode"][1]
    for elapsed in range(30, mode_interval, 30):
        clock.now = 1000 + elapsed
        await coordinator.async_refresh()
        assert coordinator.passive_power_state == "acknowledged"
        assert mock_marstek_api.get_es_mode.call_count == 1
    clock.now = 1000 + mode_interval
    await coordinator.async_refresh()
    assert mock_marstek_api.get_es_mode.call_count == 2
    clock.now += 30
    coordinator._set_passive_power_state("unknown")
    await coordinator.async_refresh()
    assert mock_marstek_api.get_es_mode.call_count == 2
    assert coordinator.passive_power_state == "unknown"
    assert coordinator._polling.profile == PROFILE_NORMAL
    clock.now += 30
    await coordinator.async_refresh()
    assert coordinator.passive_power_state == "acknowledged"
    assert mock_marstek_api.get_es_mode.call_count == 2
    assert coordinator._polling.profile == PROFILE_NORMAL


async def test_status_acknowledges_new_command_despite_mode_failure(
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
    assert coordinator.passive_power_state == "acknowledged"
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
    # ES probes obey both health and endpoint backoff; failed/stale status
    # never requests extra mode verification through the health circuit breaker.
    assert mock_marstek_api.get_es_status.call_count == 2
    mock_marstek_api.get_es_mode.assert_not_called()
    for key in ("wifi", "battery", "pv"):
        getattr(mock_marstek_api, METHODS[key]).assert_not_called()
        assert key in coordinator.data


@pytest.mark.parametrize("reason", ["failed_command", "recovery"])
async def test_stable_passive_leaves_normal_mode_polling_when_verification_needed(
    coordinator, mock_marstek_api, clock, reason
):
    mock_marstek_api.get_es_status.return_value = {"ongrid_power": 250}
    mock_marstek_api.get_es_mode.return_value = {"mode": "Passive", "ongrid_power": 250}
    await coordinator.async_set_passive_power(250)
    await coordinator.async_refresh()
    clock.now += 30
    await coordinator.async_refresh()
    assert coordinator._polling.profile == PROFILE_NORMAL
    assert coordinator._polling.effective_interval("es_mode") == ENDPOINTS["es_mode"][1]
    assert mock_marstek_api.get_es_mode.call_count == 1
    if reason == "failed_command":
        mock_marstek_api.set_es_mode_passive.return_value = False
        await coordinator.async_set_passive_power(260)
    else:
        coordinator._schedule_passive_retry("verification_retry")
    await coordinator.async_refresh()
    assert mock_marstek_api.get_es_mode.call_count == 2


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
    clock.now += ENDPOINTS["pv"][1]
    mock_marstek_api.get_pv_status.return_value = None
    await coordinator.async_refresh()
    assert coordinator.data["pv"] == {"pv1_power": 42}
    assert "pv" not in coordinator._disabled_optional_sections
    clock.now += 30
    await coordinator.async_refresh()
    assert mock_marstek_api.get_pv_status.call_count == 2
    assert coordinator.data["pv"] == {"pv1_power": 42}
    clock.now = coordinator._polling.sections["pv"].next_poll
    mock_marstek_api.get_pv_status.return_value = {"pv1_power": 100}
    await coordinator.async_refresh()
    assert coordinator.data["pv"] == {"pv1_power": 100}
    assert coordinator._polling.sections["pv"].consecutive_failures == 0


async def test_raised_timeout_uses_endpoint_backoff(coordinator, clock):
    fetch = Mock(side_effect=TimeoutError)
    assert await coordinator._fetch_section("es", fetch) is None
    _, interval, cap = ENDPOINTS["es"]
    assert coordinator._polling.sections["es"].next_poll == clock.now + min(
        cap, interval * 2
    )
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
    # The degraded probe gap still leaves ES within its freshness window, so
    # mode is read only for confirmation and post-command verification.
    clock.now += 120 if degraded else ENDPOINTS["es"][1]
    mock_marstek_api.get_es_status.side_effect = [
        {"ongrid_power": 0},
        {"ongrid_power": 0},
        {"ongrid_power": 250},
    ]
    mock_marstek_api.get_es_mode.side_effect = [
        {"mode": "Passive", "ongrid_power": 0},
        {"mode": "Passive", "ongrid_power": 250},
    ]

    async def settle():
        clock.now += 15

    monkeypatch.setattr(coordinator, "_async_wait_passive_settle", settle)
    await coordinator.async_refresh()
    mock_marstek_api.set_es_mode_passive.assert_called_once_with(250)
    assert mock_marstek_api.get_es_status.call_count == 3
    assert mock_marstek_api.get_es_mode.call_count == 2
    assert coordinator.passive_power_state == "acknowledged"
    assert coordinator.data["es"]["ongrid_power"] == 250


@pytest.mark.parametrize("raised_timeout", [False, True])
async def test_mode_failure_backoff_preserves_power_acknowledgement(
    coordinator, mock_marstek_api, clock, raised_timeout
):
    mock_marstek_api.get_es_status.return_value = {"ongrid_power": 250}
    mock_marstek_api.get_es_mode.return_value = {"mode": "Passive", "ongrid_power": 250}
    await coordinator.async_set_passive_power(250)
    await coordinator.async_refresh()
    clock.now += 30
    await coordinator.async_refresh()
    mode = coordinator._polling.sections["es_mode"]
    while clock.now + 30 < mode.next_poll:
        clock.now += 30
        await coordinator.async_refresh()
    clock.now = mode.next_poll
    mock_marstek_api.get_es_mode.side_effect = TimeoutError if raised_timeout else None
    mock_marstek_api.get_es_mode.return_value = None
    await coordinator.async_refresh()
    assert coordinator.passive_power_state == "acknowledged"
    assert coordinator._polling.profile == PROFILE_NORMAL
    failed_calls = mock_marstek_api.get_es_mode.call_count
    deadline = mode.next_poll
    assert deadline > clock.now + mode.interval
    while clock.now + 30 < deadline:
        clock.now += 30
        await coordinator.async_refresh()
        assert coordinator.passive_power_state == "acknowledged"
        assert mock_marstek_api.get_es_mode.call_count == failed_calls
        assert mode.next_poll == deadline
        assert coordinator._polling.profile == PROFILE_NORMAL
    clock.now = deadline
    mock_marstek_api.get_es_mode.side_effect = None
    mock_marstek_api.get_es_mode.return_value = {"mode": "Passive", "ongrid_power": 250}
    await coordinator.async_refresh()
    assert coordinator.passive_power_state == "acknowledged"
    assert coordinator._polling.sections["es_mode"].consecutive_failures == 0


async def test_reduced_mode_polling_preserves_calibration_samples(
    coordinator, mock_marstek_api, clock
):
    mock_marstek_api.get_es_status.return_value = {"ongrid_power": 240}
    mock_marstek_api.get_es_mode.return_value = {"mode": "Passive", "ongrid_power": 240}
    await coordinator.async_set_passive_power(240)
    clock.now += 16
    await coordinator.async_refresh()
    samples = list(coordinator._passive_samples)
    assert len(samples) == 1
    for _ in range(2):
        clock.now += 30
        await coordinator.async_refresh()
        assert coordinator.passive_power_state == "acknowledged"
        assert list(coordinator._passive_samples) == samples
    assert mock_marstek_api.get_es_mode.call_count == 1
    # ES itself is skipped on alternate ticks at its normal 60 s cadence.
    assert mock_marstek_api.get_es_status.call_count == 2


CONTROL_ENDPOINTS = {"es", "es_mode", "battery"}


def calls(api):
    return {key: getattr(api, method).call_count for key, method in METHODS.items()}


def test_normal_intervals_are_the_reduced_read_profile():
    assert {key: interval for key, (_, interval, _) in ENDPOINTS.items()} == {
        "es": 60,
        "es_mode": 120,
        "battery": 180,
        "pv": 300,
        "em": 300,
        "wifi": 900,
        "ble": 600,
    }
    assert ACTIVE_INTERVALS == {"es": 30, "es_mode": 30, "battery": 60}
    # Freshness tolerates one missed normal ES read.
    assert ES_FRESH_SECONDS == 2 * ENDPOINTS["es"][1]


def test_active_profile_shortens_only_control_endpoints():
    policy = PollingPolicy("test")
    normal = {key: policy.effective_interval(key) for key in ENDPOINTS}
    assert policy.profile == PROFILE_NORMAL
    policy.set_profile(True)
    assert policy.profile == PROFILE_ACTIVE
    assert {key: policy.effective_interval(key) for key in ENDPOINTS} == {
        **normal,
        **ACTIVE_INTERVALS,
    }
    # Backoff keeps its normal base: `interval` is never rewritten.
    assert all(policy.sections[key].interval == normal[key] for key in ENDPOINTS)


def test_active_window_is_bounded_and_cleared_by_settling():
    policy = PollingPolicy("test")
    policy.request_active(0)
    policy.update_profile(False, ACTIVE_PROFILE_MAX_SECONDS - 0.001)
    assert policy.profile == PROFILE_ACTIVE
    policy.update_profile(False, ACTIVE_PROFILE_MAX_SECONDS)
    assert policy.profile == PROFILE_NORMAL
    policy.request_active(1000)
    policy.update_profile(True, 1001)
    assert policy.profile == PROFILE_NORMAL
    # Settling consumed the trigger, so a later unsettled check stays normal.
    policy.update_profile(False, 1002)
    assert policy.profile == PROFILE_NORMAL


@pytest.mark.parametrize("accepted", [True, False])
async def test_set_mode_activates_fast_profile(
    coordinator, mock_marstek_api, clock, accepted
):
    await coordinator.async_refresh()
    assert coordinator._polling.profile == PROFILE_NORMAL
    mock_marstek_api.set_es_mode_auto.return_value = accepted
    assert await coordinator.async_set_operating_mode("Auto") is accepted
    await coordinator._fetch_section("battery", mock_marstek_api.get_battery_status)
    assert coordinator._polling.profile == PROFILE_ACTIVE
    mock_marstek_api.reset_mock()
    clock.now += ACTIVE_INTERVALS["es"]
    await coordinator.async_refresh()
    # ES is due only through the active profile; mode is verified. Battery's
    # active interval has not elapsed and slow endpoints keep their cadence.
    assert calls(mock_marstek_api) == {
        "es": 1,
        "es_mode": 1,
        "battery": 0,
        "em": 0,
        "pv": 0,
        "wifi": 0,
        "ble": 0,
    }
    # The verified mode settles control, so polling is normal again.
    assert coordinator._polling.profile == PROFILE_NORMAL


async def test_active_profile_polls_only_control_endpoints_for_bounded_window(
    coordinator, mock_marstek_api, clock, monkeypatch, caplog
):
    caplog.set_level("DEBUG")
    await coordinator.async_refresh()
    mock_marstek_api.reset_mock()
    # An output that can never be confirmed, e.g. charging refused.
    monkeypatch.setattr(coordinator, "_control_settled", lambda: False)
    coordinator._polling.request_active(clock.now)
    times = range(1030, 1000 + ACTIVE_PROFILE_MAX_SECONDS, 30)
    for now in times:
        clock.now = now
        await coordinator.async_refresh()
        assert coordinator._polling.profile == PROFILE_ACTIVE
    assert calls(mock_marstek_api) == {
        "es": len(times),
        "es_mode": len(times),
        "battery": len(times) // 2,
        "em": 0,
        "pv": 0,
        "wifi": 0,
        "ble": 0,
    }
    assert (
        "endpoint=ES.GetStatus normal_interval=60s effective_interval=30s "
        "profile=active"
    ) in caplog.text
    clock.now = 1000 + ACTIVE_PROFILE_MAX_SECONDS
    await coordinator.async_refresh()
    assert coordinator._polling.profile == PROFILE_NORMAL
    mock_marstek_api.set_es_mode_passive.assert_not_called()
    mock_marstek_api.set_es_mode_auto.assert_not_called()


async def test_passive_confirmation_returns_to_normal_polling(
    coordinator, mock_marstek_api, clock
):
    mock_marstek_api.get_es_status.return_value = {"ongrid_power": 250}
    mock_marstek_api.get_es_mode.return_value = {"mode": "Passive", "ongrid_power": 250}
    await coordinator.async_refresh()
    clock.now = 1010
    await coordinator.async_set_passive_power(250)
    assert coordinator.passive_power_state == "sent"
    await coordinator._fetch_section("battery", mock_marstek_api.get_battery_status)
    assert coordinator._polling.profile == PROFILE_ACTIVE
    mock_marstek_api.reset_mock()
    clock.now = 1030
    await coordinator.async_refresh()
    assert coordinator.passive_power_state == "acknowledged"
    assert calls(mock_marstek_api)["es"] == 1
    assert calls(mock_marstek_api)["es_mode"] == 1
    assert coordinator._polling.profile == PROFILE_NORMAL
    assert coordinator._polling.sections["es"].next_poll == 1030 + ENDPOINTS["es"][1]
    for now in (1060, 1090, 1120):
        clock.now = now
        await coordinator.async_refresh()
        assert coordinator._polling.profile == PROFILE_NORMAL
    assert calls(mock_marstek_api)["es"] == 2
    assert calls(mock_marstek_api)["es_mode"] == 1


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("key", sorted(CONTROL_ENDPOINTS))
def test_endpoint_backoff_overrides_both_profiles(key, active):
    policy = PollingPolicy("test")
    policy.set_profile(active)
    state = policy.sections[key]
    policy.record(key, True, 0)
    policy.record(key, False, 10)
    deadline = 10 + min(state.cap, state.interval * 2)
    assert state.next_poll == deadline
    policy.set_profile(not active)
    policy.set_profile(active)
    assert state.next_poll == deadline
    assert not policy.due(key, deadline - 0.001)
    assert policy.due(key, deadline)


async def test_degraded_mode_overrides_active_profile(
    coordinator, mock_marstek_api, clock, monkeypatch
):
    await coordinator.async_refresh()
    degrade(coordinator._polling, clock.now)
    monkeypatch.setattr(coordinator, "_control_settled", lambda: False)
    coordinator._polling.request_active(clock.now)
    mock_marstek_api.reset_mock()
    for elapsed in range(30, HEALTH_PROBE_INTERVAL, 30):
        clock.now = 1000 + elapsed
        await coordinator.async_refresh()
        assert coordinator._polling.profile == PROFILE_ACTIVE
        assert not any(calls(mock_marstek_api).values())
    clock.now = 1000 + HEALTH_PROBE_INTERVAL
    await coordinator.async_refresh()
    assert calls(mock_marstek_api) == {key: int(key == "es") for key in METHODS}


async def test_profile_changes_never_write_es_set_mode(
    coordinator, mock_marstek_api, clock
):
    mock_marstek_api.get_es_status.return_value = {"ongrid_power": 250}
    mock_marstek_api.get_es_mode.return_value = {"mode": "Passive", "ongrid_power": 250}
    await coordinator.async_set_passive_power(250)
    for now in range(1000, 1900, 30):
        clock.now = now
        await coordinator.async_refresh()
        assert coordinator.passive_power_state == "acknowledged"
    # Only the target itself was written; keepalive timers never fire here.
    assert mock_marstek_api.set_es_mode_passive.call_count == 1
    mock_marstek_api.set_es_mode_auto.assert_not_called()


async def test_active_profile_keeps_disabled_optional_sections_off(
    coordinator, mock_marstek_api, clock, monkeypatch
):
    mock_marstek_api.get_ble_status.return_value = None
    mock_marstek_api.get_em_status.return_value = {"ct_state": 0}
    await coordinator.async_refresh()
    assert coordinator._disabled_optional_sections == {"ble", "em"}
    monkeypatch.setattr(coordinator, "_control_settled", lambda: False)
    coordinator._polling.request_active(clock.now)
    for now in range(1030, 1000 + 2 * ENDPOINTS["ble"][1], 30):
        clock.now = now
        await coordinator.async_refresh()
    assert mock_marstek_api.get_ble_status.call_count == 1
    assert mock_marstek_api.get_em_status.call_count == 1
    assert coordinator.data["em"] == {"ct_state": 0}


@pytest.mark.parametrize("passive", [False, True])
@pytest.mark.parametrize("error", [None, TimeoutError, RuntimeError])
async def test_es_read_failure_does_not_activate_or_accelerate_other_endpoints(
    coordinator, mock_marstek_api, clock, caplog, passive, error
):
    mock_marstek_api.get_es_status.return_value = {"ongrid_power": 250}
    mock_marstek_api.get_es_mode.return_value = {"mode": "Passive", "ongrid_power": 250}
    if passive:
        await coordinator.async_set_passive_power(250)
    await coordinator.async_refresh()
    assert coordinator._polling.profile == PROFILE_NORMAL
    deadlines = {
        key: state.next_poll for key, state in coordinator._polling.sections.items()
    }
    mock_marstek_api.reset_mock()
    caplog.clear()
    caplog.set_level("DEBUG")
    mock_marstek_api.get_es_status.return_value = None
    mock_marstek_api.get_es_status.side_effect = error
    for now in (1060, 1090):
        clock.now = now
        await coordinator.async_refresh()
        assert coordinator._polling.profile == PROFILE_NORMAL
        assert not coordinator._mode_verification_required
        assert {
            key: state.next_poll
            for key, state in coordinator._polling.sections.items()
            if key != "es"
        } == {key: deadline for key, deadline in deadlines.items() if key != "es"}
    assert calls(mock_marstek_api) == {key: int(key == "es") for key in METHODS}
    assert coordinator._polling.sections["es"].next_poll == 1180
    if passive:
        assert coordinator.passive_power_state == "unknown"
    # A stale cached output during backoff must not restart control polling.
    clock.now = 1000 + ES_FRESH_SECONDS + 1
    await coordinator.async_refresh()
    assert coordinator._polling.profile == PROFILE_NORMAL
    assert "profile=active" not in caplog.text
    assert coordinator._passive_retry is None
    mock_marstek_api.set_es_mode_passive.assert_not_called()
    mock_marstek_api.set_es_mode_auto.assert_not_called()
    mock_marstek_api.get_battery_status.assert_not_called()
    assert mock_marstek_api.get_es_mode.call_count == 1  # Normal 120 s deadline.


async def test_multiple_read_failures_degrade_without_activating_control_polling(
    coordinator, mock_marstek_api, clock, caplog
):
    mock_marstek_api.get_es_status.return_value = {"ongrid_power": 250}
    mock_marstek_api.get_es_mode.return_value = {"mode": "Passive", "ongrid_power": 250}
    await coordinator.async_set_passive_power(250)
    await coordinator.async_refresh()
    assert coordinator._polling.profile == PROFILE_NORMAL
    mock_marstek_api.reset_mock()
    caplog.clear()
    caplog.set_level("DEBUG")
    for key in CONTROL_ENDPOINTS:
        getattr(mock_marstek_api, METHODS[key]).side_effect = TimeoutError
    clock.now = 1180
    await coordinator.async_refresh()
    policy = coordinator._polling
    assert policy.degraded
    for key in CONTROL_ENDPOINTS:
        state = policy.sections[key]
        assert state.consecutive_failures == 1
        assert state.next_poll == 1180 + min(state.cap, state.interval * 2)
    for now in (1210, 1240, 1270, 1300, 1330, 1360, 1390, 1420, 1450):
        clock.now = now
        await coordinator.async_refresh()
        assert policy.profile == PROFILE_NORMAL
        assert policy.degraded
        assert not coordinator._mode_verification_required
    assert calls(mock_marstek_api) == {
        key: 2 if key == "es" else int(key in CONTROL_ENDPOINTS) for key in METHODS
    }
    assert policy.sections["es"].next_poll == 1480  # Second failure reaches cap.
    assert coordinator._passive_retry is None
    assert "profile=active" not in caplog.text
    mock_marstek_api.set_es_mode_passive.assert_not_called()
    mock_marstek_api.set_es_mode_auto.assert_not_called()
