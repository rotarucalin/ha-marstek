"""Correlate UDP replies that arrive after their request timed out.

The fake socket is injected directly instead of patching `socket.socket`, so the
asynchronous coordinator tests never touch the process-wide socket module.
"""

import json
import logging
from collections import deque
from unittest.mock import patch

import pytest

from custom_components.marstek import MarstekDataUpdateCoordinator
from custom_components.marstek.marstek_api import (
    LATE_RESPONSE_WINDOW_SECONDS,
    MAX_RECENT_REQUESTS,
    MarstekAPI,
)

API_MODULE = "custom_components.marstek.marstek_api"
HOST = "192.0.2.1"


class Clock:
    """Transport clock that never delays the suite."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, delay):
        self.now += delay


class FakeSocket:
    """Deliver queued datagrams; an empty queue is a receive timeout."""

    def __init__(self, clock):
        self.clock = clock
        self.sent = []
        self.queue = deque()

    def settimeout(self, _timeout):
        pass

    def sendto(self, payload, _address):
        self.sent.append(json.loads(payload))

    def recvfrom(self, _size):
        if not self.queue:
            self.clock.now += 5.0
            raise TimeoutError
        self.clock.now += 0.1
        return self.queue.popleft(), (HOST, 30000)

    def deliver(self, request_id, result=None, **extra):
        body = {"id": request_id, **extra}
        if result is not None:
            body["result"] = result
        self.queue.append(json.dumps(body).encode())

    def close(self):
        pass


@pytest.fixture
def clock():
    clock = Clock()
    with (
        patch(f"{API_MODULE}.monotonic", clock.monotonic),
        patch(f"{API_MODULE}.sleep", clock.sleep),
    ):
        yield clock


@pytest.fixture
def api(clock):
    api = MarstekAPI(HOST)
    api._socket = FakeSocket(clock)
    return api


def messages(caplog, prefix):
    return [r.message for r in caplog.records if r.message.startswith(prefix)]


@pytest.fixture(autouse=True)
def debug_logs(caplog):
    caplog.set_level(logging.DEBUG, logger=API_MODULE)


def test_in_order_response_is_unchanged(api, caplog):
    api._socket.deliver(1, {"soc": 50})
    assert api.get_battery_status() == {"soc": 50}
    assert api.take_late_results() == {}
    assert not messages(caplog, "Accepted late response")
    assert not messages(caplog, "Ignored")
    assert api._recent[1].state == "completed"


def test_reply_after_timeout_is_correlated_to_its_request(api, clock, caplog):
    assert api.get_battery_status() is None
    assert api._recent[1].state == "timed_out"

    api._socket.deliver(1, {"soc": 51})
    api._socket.deliver(2, {"ongrid_power": 100})
    assert api.get_es_status() == {"ongrid_power": 100}

    assert api.take_late_results() == {"Bat.GetStatus": (1, {"soc": 51})}
    assert api.take_late_results() == {}
    (accepted,) = messages(caplog, "Accepted late response")
    assert "request_id=1 method=Bat.GetStatus" in accepted
    assert "age=7.6s current_request_id=2" in accepted
    assert api._recent[1].state == "late_received"


def test_late_reply_cannot_overwrite_newer_data(api, caplog):
    assert api.get_battery_status() is None  # 1 times out
    api._socket.deliver(2, {"soc": 60})
    assert api.get_battery_status() == {"soc": 60}  # 2 succeeds

    api._socket.deliver(1, {"soc": 59})
    api._socket.deliver(3, {})
    assert api.get_es_status() == {}

    assert api.take_late_results() == {}
    (stale,) = messages(caplog, "Ignored late response because newer data exists")
    assert "request_id=1 method=Bat.GetStatus" in stale
    assert "newer_request_id=2" in stale


def test_older_late_reply_is_used_until_newer_data_exists(api, caplog):
    assert api.get_battery_status() is None  # 1
    assert api.get_battery_status() is None  # 2

    api._socket.deliver(1, {"soc": 40})
    assert api.get_es_status() is None  # 3 times out after the late reply
    assert api.take_late_results() == {"Bat.GetStatus": (1, {"soc": 40})}

    api._socket.deliver(2, {"soc": 41})
    api._socket.deliver(1, {"soc": 40})  # Duplicate of an answered request.
    assert api.get_es_status() is None  # 4
    assert api.take_late_results() == {"Bat.GetStatus": (2, {"soc": 41})}
    assert messages(caplog, "Ignored response with unknown or expired request ID")


def test_matching_reply_supersedes_late_reply_in_same_exchange(api, caplog):
    assert api.get_battery_status() is None  # 1
    api._socket.deliver(1, {"soc": 30})
    api._socket.deliver(2, {"soc": 31})
    assert api.get_battery_status() == {"soc": 31}
    assert api.take_late_results() == {}
    (stale,) = messages(caplog, "Ignored late response because newer data exists")
    assert "request_id=1" in stale and "newer_request_id=2" in stale


def test_unknown_and_expired_ids_are_ignored(api, clock, caplog):
    api._socket.deliver(99, {"soc": 1})
    api._socket.deliver(1, {"soc": 2})
    assert api.get_battery_status() == {"soc": 2}

    assert api.get_battery_status() is None  # 2 times out
    clock.now += LATE_RESPONSE_WINDOW_SECONDS + 1
    api._socket.deliver(2, {"soc": 3})
    assert api.get_es_status() is None
    assert api.take_late_results() == {}

    ignored = messages(caplog, "Ignored response with unknown or expired request ID")
    assert [("response_id=99" in m, "response_id=2" in m) for m in ignored] == [
        (True, False),
        (False, True),
    ]
    assert not messages(caplog, "Accepted late response")


@pytest.mark.parametrize(
    "packet", [{"result": {"soc": 9}}, {"error": {"code": -1, "message": "bad"}}]
)
def test_id_zero_is_never_a_late_response(api, caplog, packet):
    api._request_id = -1  # The next request is sent with ID 0.
    assert api.get_battery_status() is None
    api._socket.queue.append(json.dumps({"id": 0, **packet}).encode())
    assert api.get_es_status() is None
    assert api.take_late_results() == {}
    assert messages(caplog, "Ignored malformed response with id=0")
    assert not messages(caplog, "Accepted late response")


def test_late_error_reply_is_correlated_but_not_applied(api, caplog):
    assert api.get_battery_status() is None
    api._socket.deliver(1, error={"code": -1, "message": "busy"})
    assert api.get_es_status() is None
    assert api.take_late_results() == {}
    (correlated,) = messages(caplog, "Correlated late response without applying")
    assert "request_id=1 method=Bat.GetStatus type=read" in correlated


def test_late_mode_write_is_correlated_without_changing_the_result(api, caplog):
    assert api.get_battery_status() is None  # 1, requested before the write
    assert api.set_es_mode_passive(300) is False  # 2 times out

    api._socket.deliver(2, {"set_result": True})
    api._socket.deliver(1, {"soc": 70})
    assert api.get_es_mode() is None

    assert api.take_late_results() == {}
    (write,) = messages(caplog, "Correlated late response without applying")
    assert "request_id=2 method=ES.SetMode type=write" in write
    # Control telemetry requested before the write cannot be refreshed later.
    (stale,) = messages(caplog, "Ignored late response because newer data exists")
    assert "request_id=1 method=Bat.GetStatus" in stale
    assert "newer_request_id=2" in stale
    assert len(api._socket.sent) == 3  # No retry or extra request.


def test_endpoints_are_tracked_independently(api):
    assert api.get_battery_status() is None  # 1
    assert api.get_es_status() is None  # 2
    api._socket.deliver(4, {"rssi": -50})
    assert api.get_wifi_status() is None  # 3 times out
    api._socket.deliver(2, {"ongrid_power": 6})
    api._socket.deliver(1, {"soc": 80})
    api._socket.deliver(3, {"rssi": -60})
    api._socket.deliver(4, {"rssi": -55})
    assert api.get_wifi_status() == {"rssi": -55}  # 4
    # Newer ES data does not block the older battery reply, and the newer
    # Wi-Fi reply rejects only the older Wi-Fi reply.
    assert api.take_late_results() == {
        "Bat.GetStatus": (1, {"soc": 80}),
        "ES.GetStatus": (2, {"ongrid_power": 6}),
    }


def test_invalidated_reads_reject_earlier_late_replies(api, caplog):
    assert api.get_es_status() is None  # 1
    api.invalidate_reads("ES.GetStatus")
    api._socket.deliver(1, {"ongrid_power": 1})
    assert api.get_battery_status() is None
    assert api.take_late_results() == {}
    assert "newer_request_id=1" in messages(
        caplog, "Ignored late response because newer data exists"
    )[0]


def test_registry_is_bounded_and_expires(api, clock):
    started = clock.now
    for _ in range(MAX_RECENT_REQUESTS + 10):
        api.get_battery_status()
        # Keep every entry inside the time window.
        clock.now = started
        api._next_request_at = 0.0
    assert len(api._recent) == MAX_RECENT_REQUESTS
    assert next(iter(api._recent)) == 11

    clock.now += LATE_RESPONSE_WINDOW_SECONDS + 10
    api.get_battery_status()
    assert list(api._recent) == [MAX_RECENT_REQUESTS + 11]


@pytest.fixture
def coordinator(hass, marstek_entry, api):
    marstek_entry.add_to_hass(hass)
    return MarstekDataUpdateCoordinator(hass, api, marstek_entry)


@pytest.mark.asyncio
async def test_late_read_updates_only_its_section_cache(coordinator, api):
    coordinator._last_good_data["es"] = {"ongrid_power": 1}
    assert await coordinator._async_device_job(api.get_battery_status) is None

    api._socket.deliver(1, {"soc": 55})
    await coordinator._async_device_job(api.get_es_status)

    assert coordinator._last_good_data["battery"] == {"soc": 55}
    assert coordinator._last_good_data["es"] == {"ongrid_power": 1}
    # Late data is cached, never treated as a fresh verification read.
    assert "battery" not in coordinator._fresh_sections
    assert "battery" not in coordinator._missing_cycles


@pytest.mark.asyncio
async def test_late_read_does_not_replace_newer_section_data(coordinator, api):
    await coordinator._async_device_job(api.get_battery_status)  # 1 times out
    api._socket.deliver(2, {"soc": 65})
    coordinator._last_good_data["battery"] = await coordinator._async_device_job(
        api.get_battery_status
    )
    api._socket.deliver(1, {"soc": 64})
    await coordinator._async_device_job(api.get_es_status)
    assert coordinator._last_good_data["battery"] == {"soc": 65}


@pytest.mark.asyncio
async def test_dropped_section_rejects_late_data_from_before(coordinator, api):
    await coordinator._async_device_job(api.get_es_status)  # 1 times out
    coordinator._last_good_data["es"] = {"ongrid_power": 1}
    coordinator._discard_superseded_passive_readings({})

    api._socket.deliver(1, {"ongrid_power": 2})
    await coordinator._async_device_job(api.get_battery_status)
    assert "es" not in coordinator._last_good_data


@pytest.mark.asyncio
async def test_disabled_section_ignores_late_data(coordinator, api):
    await coordinator._async_device_job(api.get_ble_status)  # 1 times out
    coordinator._disabled_optional_sections.add("ble")
    api._socket.deliver(1, {"state": "connect"})
    await coordinator._async_device_job(api.get_battery_status)
    assert "ble" not in coordinator._last_good_data
