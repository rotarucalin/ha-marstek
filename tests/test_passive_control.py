"""Deterministic regressions for maintained Passive commands and provenance."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from unittest.mock import AsyncMock, call, patch

import pytest
from homeassistant.core import HassJob, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.marstek import (
    PASSIVE_POWER_KEEPALIVE_SECONDS,
    PASSIVE_POWER_RETRY_SECONDS,
    MarstekDataUpdateCoordinator,
)
from custom_components.marstek.const import (
    CALIBRATION_STORAGE_KEY_FMT,
    CALIBRATION_STORAGE_VERSION,
    DOMAIN,
    PASSIVE_COMMAND_MAX,
    PASSIVE_DEADBAND_W,
    PASSIVE_SETTLE_SECONDS,
    PASSIVE_SOC_LEARN_MAX,
    PASSIVE_SOC_LEARN_MIN,
    PASSIVE_STABILITY_SAMPLES,
    SOURCE_CALIBRATION,
)
from custom_components.marstek.number import MarstekPassivePowerNumber
from custom_components.marstek.select import MarstekOperatingModeSelect
from custom_components.marstek.services import async_register_services

pytestmark = pytest.mark.asyncio
LOGGER = "custom_components.marstek"


@dataclass
class ScheduledCommand:
    """A timer whose callback can also model an already queued job."""

    hass: HomeAssistant
    delay: float
    action: Callable
    active: bool = True

    def cancel(self):
        self.active = False

    def fire(self):
        self.active = False
        return self.hass.async_run_hass_job(HassJob(self.action), None)


class CommandTimers:
    """Record timer replacements without waiting for wall-clock time."""

    def __init__(self):
        self.history = []

    @property
    def active(self):
        return [timer for timer in self.history if timer.active]

    @property
    def current(self):
        assert len(self.active) == 1
        return self.active[0]

    def schedule(self, hass, delay, action):
        # Fail on overlap even if a later cancellation would hide it.
        assert not self.active
        timer = ScheduledCommand(hass, delay, action)
        self.history.append(timer)
        return timer.cancel


@pytest.fixture
def command_timers():
    timers = CommandTimers()
    with patch("custom_components.marstek.async_call_later", timers.schedule):
        yield timers


@pytest.fixture
async def coordinator(hass, marstek_entry, mock_marstek_api, command_timers, caplog):
    marstek_entry.add_to_hass(hass)
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    coordinator = MarstekDataUpdateCoordinator(hass, mock_marstek_api, marstek_entry)
    yield coordinator
    await coordinator.async_stop_passive_control()


def outgoing_messages(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("Marstek command:")
    ]


def passive_power_messages(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("Marstek passive power:")
    ]


class FakeClock:
    """A settable stand-in for time.monotonic driving settle/stability windows."""

    def __init__(self, start: float = 1_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    fake = FakeClock()
    with patch("custom_components.marstek.monotonic", fake):
        yield fake


async def _settle_and_sample(coordinator, clock, actual, count=PASSIVE_STABILITY_SAMPLES):
    """Advance past the settle window and feed `count` stable measurements.

    Mirrors one learning round: a poll cadence of stable telemetry taken after
    the command has had time to settle.
    """
    clock.advance(PASSIVE_SETTLE_SECONDS + 1)
    for _ in range(count):
        await coordinator._async_verify_passive_power(
            {"mode": "Passive", "ongrid_power": actual},
            es_data={"ongrid_power": actual},
            battery_data={"soc": 50},
            fresh=frozenset({"es_mode", "es", "battery"}),
        )
        clock.advance(1)


@pytest.mark.parametrize("success", [True, False])
async def test_real_timer_runs_keepalive_and_retry_on_event_loop(
    hass, marstek_entry, mock_marstek_api, freezer, success
):
    """HA must dispatch both timer paths without calling async APIs in a worker."""
    marstek_entry.add_to_hass(hass)
    coordinator = MarstekDataUpdateCoordinator(hass, mock_marstek_api, marstek_entry)
    mock_marstek_api.set_es_mode_passive.return_value = success
    try:
        assert await coordinator.async_set_passive_power(240) is success
        mock_marstek_api.set_es_mode_passive.return_value = True
        delay = (
            PASSIVE_POWER_KEEPALIVE_SECONDS if success else PASSIVE_POWER_RETRY_SECONDS
        )
        freezer.tick(timedelta(seconds=delay + 1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert mock_marstek_api.set_es_mode_passive.call_args_list == [
            call(240),
            call(240),
        ]
        assert coordinator.passive_power_state == "sent"
        assert coordinator._passive_keepalive_cancel is not None
    finally:
        await coordinator.async_stop_passive_control()


async def test_new_target_and_successful_keepalive(
    coordinator, command_timers, mock_marstek_api, caplog
):
    """Accepted commands use the unchanged 180s cadence and distinct sources."""
    assert PASSIVE_POWER_KEEPALIVE_SECONDS == 180
    assert PASSIVE_POWER_RETRY_SECONDS == 15
    assert await coordinator.async_set_passive_power(240)
    assert command_timers.current.delay == 180
    assert coordinator.passive_power_state == "sent"
    await command_timers.current.fire()
    assert command_timers.current.delay == 180
    assert mock_marstek_api.set_es_mode_passive.call_args_list == [call(240), call(240)]
    messages = outgoing_messages(caplog)
    assert len(messages) == 2
    assert "source=new_target" in messages[0]
    assert "source=keepalive" in messages[1]
    for message in messages:
        assert "device=Marstek Venus A" in message
        assert "device_id=AA:BB:CC:DD:EE:01" in message
        assert "host=192.0.2.1 port=30000" in message
        assert "method=ES.SetMode mode=Passive power=240W" in message
    assert "Marstek command succeeded:" in caplog.text
    assert "delay=180s" in caplog.text
    assert all(record.levelno == logging.DEBUG for record in caplog.records)


@pytest.mark.parametrize(
    "failure", [False, TimeoutError("timed out"), OSError("offline")]
)
async def test_failed_keepalives_retry_until_success(
    coordinator, command_timers, mock_marstek_api, caplog, failure
):
    """All failure forms retain the target and retry at 15s until accepted."""
    mock_marstek_api.set_es_mode_passive.side_effect = [True, failure, failure, True]
    assert await coordinator.async_set_passive_power(240)
    caplog.clear()
    await command_timers.current.fire()
    assert command_timers.current.delay == 15
    assert coordinator._passive_desired_power == 240
    assert "source=keepalive " in outgoing_messages(caplog)[0]
    assert "Marstek command succeeded:" not in caplog.text
    assert "retry in 15s" in caplog.text
    assert "source=keepalive_retry mode=Passive power=240W delay=15s" in caplog.text

    caplog.clear()
    await command_timers.current.fire()
    assert command_timers.current.delay == 15
    assert coordinator._passive_desired_power == 240
    assert "source=keepalive_retry" in outgoing_messages(caplog)[0]
    assert "Marstek command succeeded:" not in caplog.text

    caplog.clear()
    await command_timers.current.fire()
    assert command_timers.current.delay == 180
    assert "source=keepalive_retry" in outgoing_messages(caplog)[0]
    assert "Marstek command succeeded:" in caplog.text
    assert mock_marstek_api.set_es_mode_passive.call_args_list == [call(240)] * 4
    mock_marstek_api.set_es_mode_auto.assert_not_called()


async def test_failed_initial_target_also_retries(
    coordinator, command_timers, mock_marstek_api, caplog
):
    mock_marstek_api.set_es_mode_passive.return_value = False
    assert not await coordinator.async_set_passive_power(-500)
    assert coordinator._passive_desired_power == -500
    assert command_timers.current.delay == 15
    assert "source=new_target" in outgoing_messages(caplog)[0]


@pytest.mark.parametrize("new_success", [True, False])
async def test_new_target_supersedes_pending_retry(
    coordinator, command_timers, mock_marstek_api, new_success
):
    mock_marstek_api.set_es_mode_passive.side_effect = [True, False, new_success, True]
    assert await coordinator.async_set_passive_power(240)
    await command_timers.current.fire()
    old_retry = command_timers.current
    assert await coordinator.async_set_passive_power(300) is new_success
    assert not old_retry.active
    replacement = command_timers.current
    assert replacement.delay == (180 if new_success else 15)
    await old_retry.fire()  # Already dispatched before cancellation.
    assert command_timers.current is replacement
    assert mock_marstek_api.set_es_mode_passive.call_args_list == [
        call(240),
        call(240),
        call(300),
    ]
    await replacement.fire()
    mock_marstek_api.set_es_mode_passive.assert_called_with(300)
    assert command_timers.current.delay == 180


@pytest.mark.parametrize("mode", ["Auto", "AI", "Manual"])
@pytest.mark.parametrize("success", [True, False])
async def test_select_stops_retries_even_if_mode_command_fails(
    coordinator, command_timers, mock_marstek_api, caplog, mode, success
):
    """An intentional mode change cannot resurrect Passive, even on failure."""
    mock_marstek_api.set_es_mode_passive.return_value = False
    await coordinator.async_set_passive_power(240)
    retry = command_timers.current
    command = getattr(mock_marstek_api, f"set_es_mode_{mode.lower()}")
    command.return_value = success
    caplog.clear()
    with patch.object(
        coordinator, "async_request_refresh", new_callable=AsyncMock
    ) as refresh:
        await MarstekOperatingModeSelect(coordinator).async_select_option(mode)
        assert refresh.await_count == int(success)
    assert not command_timers.active
    assert coordinator._passive_desired_power is None
    assert coordinator.passive_power_state == "unknown"
    await retry.fire()
    await coordinator._async_verify_passive_power({"mode": "Auto"})
    assert not command_timers.active
    mock_marstek_api.set_es_mode_passive.assert_called_once_with(240)
    if mode == "Manual":
        command.assert_called_once_with(0, "00:00", "23:59", 127, 100, 1)
    else:
        command.assert_called_once_with()
    assert f"source=operating_mode_select method=ES.SetMode mode={mode}" in caplog.text
    failures = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(failures) == int(not success)


async def test_selecting_passive_does_not_send_or_replace_target(
    coordinator, command_timers, mock_marstek_api, caplog
):
    await coordinator.async_set_passive_power(240)
    timer = command_timers.current
    mock_marstek_api.set_es_mode_passive.reset_mock()
    caplog.clear()
    with patch.object(coordinator, "async_request_refresh", new_callable=AsyncMock):
        await MarstekOperatingModeSelect(coordinator).async_select_option("Passive")
    assert command_timers.current is timer
    assert coordinator._passive_desired_power == 240
    mock_marstek_api.set_es_mode_passive.assert_not_called()
    assert not outgoing_messages(caplog)


@pytest.mark.parametrize("success", [True, False])
async def test_verification_resend_replaces_timer(
    coordinator, command_timers, mock_marstek_api, caplog, success
):
    """Fresh unconfirmed telemetry resends the target, with its own provenance."""
    await coordinator.async_set_passive_power(240)
    old_timer = command_timers.current
    mock_marstek_api.set_es_mode_passive.return_value = success
    caplog.clear()
    await coordinator._async_verify_passive_power({"mode": "Auto", "ongrid_power": 0})
    assert not old_timer.active
    replacement = command_timers.current
    assert replacement.delay == (180 if success else 15)
    assert coordinator._passive_desired_power == 240
    assert "source=verification_retry" in outgoing_messages(caplog)[0]
    assert coordinator.passive_power_state == ("retrying" if success else "sent")
    await old_timer.fire()
    assert command_timers.current is replacement
    assert mock_marstek_api.set_es_mode_passive.call_count == 2


async def test_poll_confirmation_and_cached_data_do_not_resend(
    coordinator, command_timers, mock_marstek_api, caplog
):
    await coordinator.async_set_passive_power(240)
    timer = command_timers.current
    caplog.clear()
    mock_marstek_api.get_es_mode.return_value = {"mode": "Passive", "ongrid_power": 230}
    await coordinator.async_refresh()
    assert coordinator.passive_power_state == "acknowledged"
    assert command_timers.current is timer
    mock_marstek_api.get_es_mode.return_value = None
    coordinator._last_good_data["es_mode"] = {"mode": "Auto", "ongrid_power": 0}
    await coordinator.async_refresh()
    assert command_timers.current is timer
    assert not outgoing_messages(caplog)


async def test_poll_mismatch_uses_verification_provenance(
    coordinator, command_timers, mock_marstek_api, caplog
):
    await coordinator.async_set_passive_power(240)
    caplog.clear()
    mock_marstek_api.get_es_mode.return_value = {"mode": "Passive", "ongrid_power": 0}
    await coordinator.async_refresh()
    assert "source=verification_retry" in outgoing_messages(caplog)[0]
    mock_marstek_api.set_es_mode_passive.assert_called_with(240)
    assert command_timers.current.delay == 180


@pytest.mark.parametrize("action", ["new_target", "verification", "Auto", "stop"])
async def test_callback_queued_behind_superseding_command_is_invalidated(
    coordinator, command_timers, mock_marstek_api, action
):
    """A callback waiting for the lock cannot send or discard a newer timer."""
    mock_marstek_api.set_es_mode_passive.return_value = False
    await coordinator.async_set_passive_power(240)
    timer = command_timers.current
    mock_marstek_api.set_es_mode_passive.reset_mock()
    mock_marstek_api.set_es_mode_passive.return_value = True
    await coordinator._passive_command_lock.acquire()
    try:
        if action == "new_target":
            command = coordinator.async_set_passive_power(300)
        elif action == "verification":
            command = coordinator._async_verify_passive_power({"mode": "Auto"})
        elif action == "Auto":
            command = coordinator.async_set_operating_mode("Auto")
        else:
            command = coordinator.async_stop_passive_control()
        superseding = asyncio.create_task(command)
        await asyncio.sleep(0)
        queued = timer.fire()
        await asyncio.sleep(0)
    finally:
        coordinator._passive_command_lock.release()
    await asyncio.gather(superseding, queued)
    if action in ("new_target", "verification"):
        mock_marstek_api.set_es_mode_passive.assert_called_once_with(
            300 if action == "new_target" else 240
        )
        assert command_timers.current.delay == 180
    else:
        mock_marstek_api.set_es_mode_passive.assert_not_called()
        assert not command_timers.active
        assert coordinator._passive_desired_power is None


@pytest.mark.parametrize("success", [True, False])
async def test_stop_waits_for_in_flight_send_and_cancels_its_timer(
    hass, coordinator, command_timers, mock_marstek_api, success
):
    await coordinator.async_set_passive_power(240)
    started, finish = asyncio.Event(), asyncio.Event()

    async def send(command, *args):
        assert command == mock_marstek_api.set_es_mode_passive
        started.set()
        await finish.wait()
        return success

    with patch.object(hass, "async_add_executor_job", side_effect=send):
        keepalive = command_timers.current.fire()
        await started.wait()
        stop = asyncio.create_task(coordinator.async_stop_passive_control())
        try:
            await asyncio.sleep(0)
            assert not stop.done()
        finally:
            finish.set()
            await asyncio.gather(keepalive, stop)
    assert not command_timers.active
    assert coordinator._passive_keepalive_cancel is None
    assert coordinator._passive_desired_power is None


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize("success", [True, False])
async def test_integration_unload_cancels_timer(
    hass, marstek_entry, mock_marstek_api, command_timers, success
):
    marstek_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(marstek_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][marstek_entry.entry_id]
    mock_marstek_api.set_es_mode_passive.return_value = success
    await coordinator.async_set_passive_power(240)
    timer = command_timers.current
    assert await hass.config_entries.async_unload(marstek_entry.entry_id)
    assert not command_timers.active
    assert coordinator._passive_keepalive_cancel is None
    await timer.fire()
    mock_marstek_api.set_es_mode_passive.assert_called_once_with(240)
    assert not command_timers.active


@pytest.mark.parametrize("success", [True, False])
async def test_service_behavior_and_logging(
    hass, coordinator, command_timers, mock_marstek_api, caplog, success
):
    """The service schema, cd_time compatibility and return behavior stay."""
    hass.data[DOMAIN] = {coordinator.entry.entry_id: coordinator}
    entity = er.async_get(hass).async_get_or_create(
        "select", DOMAIN, "mode", config_entry=coordinator.entry
    )
    await async_register_services(hass)
    mock_marstek_api.set_es_mode_passive.return_value = success
    await hass.services.async_call(
        DOMAIN,
        "set_operating_mode_passive",
        {"entity_id": entity.entity_id, "power": -500, "cd_time": 86400},
        blocking=True,
    )
    mock_marstek_api.set_es_mode_passive.assert_called_once_with(-500)
    assert coordinator._passive_desired_power == -500
    assert command_timers.current.delay == (180 if success else 15)
    assert "source=new_target" in outgoing_messages(caplog)[0]
    failures = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(failures) == int(not success)


async def test_number_keeps_requested_target_separate_from_measured_output(
    coordinator, mock_marstek_api, caplog
):
    number = MarstekPassivePowerNumber(coordinator)
    coordinator.data = {"es_mode": {"mode": "Passive", "ongrid_power": 215}}
    mock_marstek_api.set_es_mode_passive.return_value = False
    await number.async_set_native_value(300)
    assert coordinator._passive_desired_power == 300
    assert number.native_value == 300
    assert "source=new_target" in outgoing_messages(caplog)[0]
    coordinator.data = {"es_mode": {"mode": "Auto", "ongrid_power": 215}}
    assert number.native_value is None


# --- Adaptive passive-power compensation -----------------------------------


async def test_no_calibration_sends_desired_directly(
    coordinator, command_timers, mock_marstek_api, caplog
):
    """With nothing learned yet, the command sent equals the desired output."""
    assert await coordinator.async_set_passive_power(240)
    assert coordinator.desired_power == 240
    assert coordinator.command_power == 240
    mock_marstek_api.set_es_mode_passive.assert_called_once_with(240)
    messages = passive_power_messages(caplog)
    assert len(messages) == 1
    assert "desired=240W command=240W" in messages[0]
    assert "source=direct" in messages[0]


async def test_convergence_toward_desired_output(
    coordinator, command_timers, mock_marstek_api, clock
):
    """A constant loss is learned in one step and the deadband then holds it."""
    loss = 100

    def actual_for(command):
        return command - loss

    assert await coordinator.async_set_passive_power(240)
    assert coordinator.command_power == 240

    # Two unsettled/insufficient samples merely retry the unchanged command...
    await _settle_and_sample(coordinator, clock, actual_for(240), count=2)
    assert coordinator.command_power == 240
    # ...the third stable sample is enough to learn and resend.
    for _ in range(1):
        await coordinator._async_verify_passive_power(
            {"mode": "Passive", "ongrid_power": actual_for(240)},
            es_data={"ongrid_power": actual_for(240)},
            battery_data={"soc": 50},
            fresh=frozenset({"es_mode", "es", "battery"}),
        )
        clock.advance(1)

    learned_command = coordinator.command_power
    assert learned_command != 240
    mock_marstek_api.set_es_mode_passive.assert_called_with(learned_command)

    # A second settled round at the learned command should now be within the
    # deadband and require no further correction.
    await _settle_and_sample(coordinator, clock, actual_for(learned_command))
    assert coordinator.command_power == learned_command
    assert abs(actual_for(learned_command) - 240) <= PASSIVE_DEADBAND_W
    assert coordinator.passive_power_state == "acknowledged"


async def test_deadband_leaves_a_known_command_unchanged(
    coordinator, command_timers, mock_marstek_api, clock
):
    """A small error around a learned bucket must not perturb it."""
    coordinator.calibration.observe(240, 240, 205)  # seeds bucket 240 -> 275
    assert await coordinator.async_set_passive_power(240)
    assert coordinator.command_power == 275
    timer = command_timers.current
    mock_marstek_api.set_es_mode_passive.reset_mock()

    # An 8W error is inside the deadband.
    await _settle_and_sample(coordinator, clock, actual=240 - 8)

    assert coordinator.command_power == 275
    assert coordinator.calibration.command_for(240) == (275, SOURCE_CALIBRATION)
    assert command_timers.current is timer
    mock_marstek_api.set_es_mode_passive.assert_not_called()


async def test_keepalive_resends_learned_command_not_desired(
    coordinator, command_timers, mock_marstek_api
):
    """The maintained keepalive must resend the compensated value, not desired."""
    coordinator.calibration.observe(240, 240, 205)  # seeds bucket 240 -> 275
    assert await coordinator.async_set_passive_power(240)
    assert coordinator.desired_power == 240
    assert coordinator.command_power == 275
    mock_marstek_api.set_es_mode_passive.assert_called_once_with(275)

    mock_marstek_api.set_es_mode_passive.reset_mock()
    await command_timers.current.fire()
    mock_marstek_api.set_es_mode_passive.assert_called_once_with(275)


async def test_new_desired_power_recomputes_command(
    coordinator, command_timers, mock_marstek_api
):
    """Changing the desired output derives a fresh command, not the old one."""
    coordinator.calibration.observe(240, 240, 205)  # seeds bucket 240 -> 275
    assert await coordinator.async_set_passive_power(240)
    assert coordinator.command_power == 275

    assert await coordinator.async_set_passive_power(300)  # no calibration for 300
    assert coordinator.command_power == 300
    mock_marstek_api.set_es_mode_passive.assert_called_with(300)


async def test_stale_keepalive_cannot_override_newer_compensation_command(
    coordinator, command_timers, mock_marstek_api, clock
):
    """A retry queued before a learning step must not resend the old command."""
    assert await coordinator.async_set_passive_power(240)

    # Two priming polls accumulate samples but are not yet enough to learn;
    # each also resends the unconfirmed, unchanged command via a fresh timer.
    await _settle_and_sample(coordinator, clock, actual=100, count=PASSIVE_STABILITY_SAMPLES - 1)
    stale_timer = command_timers.current
    mock_marstek_api.set_es_mode_passive.reset_mock()
    mock_marstek_api.set_es_mode_passive.return_value = True

    await coordinator._passive_command_lock.acquire()
    try:
        compensating = asyncio.create_task(
            coordinator._async_verify_passive_power(
                {"mode": "Passive", "ongrid_power": 100},
                es_data={"ongrid_power": 100},
                battery_data={"soc": 50},
                fresh=frozenset({"es_mode", "es", "battery"}),
            )
        )
        await asyncio.sleep(0)
        queued_stale = stale_timer.fire()
        await asyncio.sleep(0)
    finally:
        coordinator._passive_command_lock.release()

    await asyncio.gather(compensating, queued_stale)

    learned_command = coordinator.command_power
    assert learned_command != 240
    mock_marstek_api.set_es_mode_passive.assert_called_once_with(learned_command)
    assert command_timers.current.delay == 180


async def test_saturation_stops_growing_and_warns_once(
    coordinator, command_timers, mock_marstek_api, clock, caplog
):
    """A command pinned at the device limit stops growing and warns once."""
    assert await coordinator.async_set_passive_power(2980)

    await _settle_and_sample(coordinator, clock, actual=2000)
    assert coordinator.command_power == PASSIVE_COMMAND_MAX

    def saturation_warnings():
        return [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "saturated" in record.getMessage()
        ]

    caplog.clear()
    await _settle_and_sample(coordinator, clock, actual=2000)
    assert coordinator.command_power == PASSIVE_COMMAND_MAX
    assert len(saturation_warnings()) == 1

    caplog.clear()
    await _settle_and_sample(coordinator, clock, actual=2000)
    assert coordinator.command_power == PASSIVE_COMMAND_MAX
    assert not saturation_warnings()


@pytest.mark.parametrize(
    "gate",
    [
        "mode_not_passive",
        "not_settled",
        "too_few_samples",
        "unstable_samples",
        "failed_send",
        "stale_es_mode",
        "stale_es",
        "soc_blocked_discharge",
        "soc_blocked_charge",
        "below_min_learn_power",
    ],
)
async def test_validity_gate_blocks_learning(
    coordinator, command_timers, mock_marstek_api, clock, gate
):
    """Each blocker leaves the calibration map untouched."""
    desired = 240
    battery = {"soc": 50}
    fresh = frozenset({"es_mode", "es", "battery"})
    mode = "Passive"
    actual_values = [205, 205, 205]
    samples_to_send = PASSIVE_STABILITY_SAMPLES

    if gate == "below_min_learn_power":
        desired = 10
        actual_values = [0, 0, 0]
    elif gate == "soc_blocked_discharge":
        battery = {"soc": PASSIVE_SOC_LEARN_MIN}
    elif gate == "soc_blocked_charge":
        desired = -240
        battery = {"soc": PASSIVE_SOC_LEARN_MAX}
        actual_values = [-205, -205, -205]
    elif gate == "unstable_samples":
        actual_values = [180, 230, 180]
    elif gate == "too_few_samples":
        samples_to_send = PASSIVE_STABILITY_SAMPLES - 1

    assert await coordinator.async_set_passive_power(desired)

    if gate == "failed_send":
        mock_marstek_api.set_es_mode_passive.return_value = False
        assert not await coordinator.async_set_passive_power(desired)

    if gate != "not_settled":
        clock.advance(PASSIVE_SETTLE_SECONDS + 1)

    call_fresh = fresh
    if gate == "stale_es_mode":
        call_fresh = frozenset({"es", "battery"})
    elif gate == "stale_es":
        call_fresh = frozenset({"es_mode", "battery"})

    for actual in actual_values[:samples_to_send]:
        mode_data = {
            "mode": "Auto" if gate == "mode_not_passive" else mode,
            "ongrid_power": actual,
        }
        await coordinator._async_verify_passive_power(
            mode_data,
            es_data={"ongrid_power": actual},
            battery_data=battery,
            fresh=call_fresh,
        )
        clock.advance(1)

    assert coordinator.calibration.is_empty


async def test_calibration_persists_across_restart(hass, marstek_entry, mock_marstek_api):
    """A learned mapping survives an unload/reload cycle via the Store."""
    marstek_entry.add_to_hass(hass)
    key = CALIBRATION_STORAGE_KEY_FMT.format(entry_id=marstek_entry.entry_id)

    store = Store(hass, CALIBRATION_STORAGE_VERSION, key)
    coordinator = MarstekDataUpdateCoordinator(
        hass, mock_marstek_api, marstek_entry, store=store, calibration_data=None
    )
    coordinator.calibration.observe(240, 240, 205)  # learns 275
    await coordinator.async_save_calibration()
    await coordinator.async_stop_passive_control()

    reloaded_store = Store(hass, CALIBRATION_STORAGE_VERSION, key)
    restored_data = await reloaded_store.async_load()
    new_coordinator = MarstekDataUpdateCoordinator(
        hass,
        mock_marstek_api,
        marstek_entry,
        store=reloaded_store,
        calibration_data=restored_data,
    )
    assert new_coordinator.calibration.command_for(240) == (275, SOURCE_CALIBRATION)

    assert await new_coordinator.async_set_passive_power(240)
    mock_marstek_api.set_es_mode_passive.assert_called_once_with(275)
    await new_coordinator.async_stop_passive_control()
