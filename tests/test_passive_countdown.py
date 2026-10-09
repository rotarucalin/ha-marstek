"""Passive keepalive retries must not let the device's cd_time expire."""

from unittest.mock import patch

import pytest
from homeassistant.core import HassJob

from custom_components.marstek import MarstekDataUpdateCoordinator
from custom_components.marstek.const import (
    DEFAULT_PASSIVE_KEEPALIVE_SECONDS,
    PASSIVE_CD_TIME_SECONDS,
    PASSIVE_COUNTDOWN_MARGIN_SECONDS,
)

pytestmark = pytest.mark.asyncio

START = 1_000.0
MARGIN_START = START + PASSIVE_CD_TIME_SECONDS - PASSIVE_COUNTDOWN_MARGIN_SECONDS
EXPIRY = START + PASSIVE_CD_TIME_SECONDS


class Timer:
    """One captured async_call_later callback, fired on demand."""

    def __init__(self, hass, delay, action):
        self.hass, self.delay, self.action, self.active = hass, delay, action, True

    def cancel(self):
        self.active = False

    def fire(self):
        self.active = False
        return self.hass.async_run_hass_job(HassJob(self.action), None)


class Timers:
    def __init__(self):
        self.history = []

    @property
    def current(self):
        active = [timer for timer in self.history if timer.active]
        assert len(active) == 1
        return active[0]

    def schedule(self, hass, delay, action):
        timer = Timer(hass, delay, action)
        self.history.append(timer)
        return timer.cancel


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now


@pytest.fixture
async def setup(hass, marstek_entry, mock_marstek_api):
    timers, clock = Timers(), Clock()
    with (
        patch("custom_components.marstek.async_call_later", timers.schedule),
        patch("custom_components.marstek.monotonic", clock),
    ):
        marstek_entry.add_to_hass(hass)
        coordinator = MarstekDataUpdateCoordinator(
            hass, mock_marstek_api, marstek_entry
        )
        yield coordinator, mock_marstek_api, timers, clock
        await coordinator.async_stop_passive_control()


def degrade(coordinator, now):
    for key in ("battery", "pv", "wifi"):
        coordinator._polling.record(key, False, now)
    assert coordinator._polling.degraded


async def fail_keepalive_while_degraded(coordinator, api, timers, clock):
    """t=0 accepted target, then a lost keepalive while the API is degraded."""
    assert await coordinator.async_set_passive_power(800)
    assert timers.current.delay == DEFAULT_PASSIVE_KEEPALIVE_SECONDS
    degrade(coordinator, clock.now)
    clock.now = START + DEFAULT_PASSIVE_KEEPALIVE_SECONDS
    api.set_es_mode_passive.return_value = False
    await timers.current.fire()
    assert api.set_es_mode_passive.call_count == 2
    # Only the retry is left; there is no further scheduled keepalive.
    assert coordinator._passive_keepalive_cancel is None
    assert coordinator._passive_retry is not None


async def fire_until(timers, clock, deadline):
    """Fire the single pending timer chain up to `deadline`; return fire times."""
    fired = []
    while clock.now + timers.current.delay <= deadline:
        timer = timers.current
        clock.now += timer.delay
        fired.append(clock.now)
        await timer.fire()
    return fired


async def test_degraded_retry_is_held_back_outside_the_countdown_margin(setup):
    coordinator, api, timers, clock = setup
    await fail_keepalive_while_degraded(coordinator, api, timers, clock)
    api.set_es_mode_passive.return_value = True

    fired = await fire_until(timers, clock, MARGIN_START - 1)

    assert len(fired) > 50
    assert api.set_es_mode_passive.call_count == 2
    assert coordinator._polling.degraded


async def test_degraded_retry_is_sent_before_the_countdown_expires(setup, caplog):
    coordinator, api, timers, clock = setup
    await fail_keepalive_while_degraded(coordinator, api, timers, clock)
    api.set_es_mode_passive.return_value = True

    while api.set_es_mode_passive.call_count == 2 and clock.now < EXPIRY:
        timer = timers.current
        clock.now += timer.delay
        await timer.fire()

    assert api.set_es_mode_passive.call_count == 3
    sent_at = clock.now
    assert MARGIN_START <= sent_at < EXPIRY
    assert "keepalive retry sent despite degraded API" in caplog.text
    # The accepted write restarts the countdown and normal keepalives.
    assert coordinator._passive_last_ack_at == sent_at
    assert coordinator._passive_retry is None
    assert timers.current.delay == DEFAULT_PASSIVE_KEEPALIVE_SECONDS
    assert coordinator._passive_keepalive_retry_index == 0


async def test_failed_degraded_retries_keep_trying_until_expiry(setup):
    coordinator, api, timers, clock = setup
    await fail_keepalive_while_degraded(coordinator, api, timers, clock)

    fired = await fire_until(timers, clock, EXPIRY)

    attempts = api.set_es_mode_passive.call_count - 2
    # Bounded backoff (30/60/90/120 s) gives several writes inside the margin.
    assert attempts >= 4
    in_margin = [t for t in fired if t >= MARGIN_START]
    assert len(in_margin) == attempts
    assert all(b - a <= 120 for a, b in zip(in_margin, in_margin[1:]))


async def test_countdown_restarts_from_each_accepted_keepalive(setup):
    coordinator, _, timers, clock = setup
    assert await coordinator.async_set_passive_power(800)
    clock.now = START + DEFAULT_PASSIVE_KEEPALIVE_SECONDS
    await timers.current.fire()
    assert coordinator._passive_last_ack_at == clock.now
    assert coordinator._passive_countdown_remaining() == PASSIVE_CD_TIME_SECONDS


async def test_leaving_passive_forgets_the_countdown(setup):
    coordinator, _, _, _ = setup
    assert await coordinator.async_set_passive_power(800)
    assert coordinator._passive_last_ack_at == START
    assert await coordinator.async_set_operating_mode("Auto")
    assert coordinator._passive_last_ack_at is None
    assert coordinator._passive_countdown_remaining() == 0
