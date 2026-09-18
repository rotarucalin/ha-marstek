"""Regressions for serialized UDP traffic and completion-based quiet periods."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import MagicMock, patch

import pytest

from custom_components.marstek.marstek_api import MarstekAPI

API_MODULE = "custom_components.marstek.marstek_api"


class TransportClock:
    """Advance only the transport clock, without delaying the test suite."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay


@pytest.fixture
def transport_clock():
    clock = TransportClock()
    with (
        patch(f"{API_MODULE}.monotonic", clock.monotonic),
        patch(f"{API_MODULE}.sleep", clock.sleep),
    ):
        yield clock


@pytest.mark.parametrize("idle", [0.0, 1.0, 4.0])
@pytest.mark.parametrize(
    "reply",
    [
        b'{"result":{"soc":50}}',
        b'{"result":{}}',
        b'{"error":{"code":-32700,"message":"Parse error"}}',
        b"invalid json",
        TimeoutError(),
        OSError("offline"),
    ],
    ids=["success", "empty", "api_error", "invalid_json", "timeout", "socket_error"],
)
def test_gap_starts_after_completion_even_on_failure(transport_clock, reply, idle):
    """Reply latency, timeout duration and idle time all precede the next send."""
    api = MarstekAPI("192.0.2.1")
    sent_at = []

    def receive(_size):
        transport_clock.now += 5.0 if isinstance(reply, Exception) else 0.2
        if isinstance(reply, Exception):
            raise reply
        return reply, (api.host, api.port)

    def close(*_args):
        transport_clock.now += 0.1
        return False

    with patch(f"{API_MODULE}.socket.socket") as socket:
        connection = socket.return_value.__enter__.return_value
        connection.sendto.side_effect = lambda *_args: sent_at.append(
            transport_clock.now
        )
        connection.recvfrom.side_effect = receive
        socket.return_value.__exit__.side_effect = close

        result = api.get_battery_status()
        assert result == (
            json.loads(reply)["result"]
            if isinstance(reply, bytes) and reply.startswith(b'{"result"')
            else None
        )
        assert sent_at == [0.0]  # No initial delay or immediate retry.
        completed_at = transport_clock.now
        transport_clock.now += idle
        api.get_battery_status()

    assert sent_at[1] == pytest.approx(completed_at + max(2.5, idle))
    assert transport_clock.sleeps == pytest.approx([2.5 - idle] if idle < 2.5 else [])


@pytest.mark.parametrize("failure_at", ["open", "send", "discovery_decode"])
def test_transport_exceptions_leave_a_quiet_gap(transport_clock, failure_at):
    """Early socket failures and failed discovery cannot bypass the cooldown."""
    api = MarstekAPI("192.0.2.1")
    with patch(f"{API_MODULE}.socket.socket") as socket:
        connection = socket.return_value.__enter__.return_value
        connection.recvfrom.return_value = (b"invalid", (api.host, api.port))
        if failure_at == "open":
            socket.side_effect = OSError("cannot open socket")
        elif failure_at == "send":
            connection.sendto.side_effect = OSError("cannot send")

        if failure_at == "discovery_decode":
            assert api.discover_devices() == []
        else:
            assert api.get_battery_status() is None

        socket.side_effect = None
        connection.sendto.side_effect = None
        connection.recvfrom.return_value = (b'{"result":{}}', (api.host, api.port))
        assert api.get_battery_status() == {}
        assert transport_clock.sleeps == [2.5]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("poll", "command"),
        ("command", "poll"),
        ("poll", "poll"),
        ("discover", "command"),
        ("poll", "discover"),
    ],
)
@pytest.mark.parametrize("first_fails", [False, True])
def test_concurrent_calls_share_one_transport_slot(
    transport_clock, first, second, first_fails
):
    """A competing worker must wait for socket close and the full quiet gap."""
    api = MarstekAPI("192.0.2.1")
    receiving = Event()
    release_reply = Event()
    contending = Event()
    underlying_lock = api._request_lock

    class ObservedLock:
        """Signal a real acquisition attempt, avoiding thread-scheduling guesses."""

        def __enter__(self):
            if receiving.is_set():
                contending.set()
            underlying_lock.acquire()

        def __exit__(self, *_args):
            underlying_lock.release()

    api._request_lock = ObservedLock()
    sent = []
    closed_at = []
    connections = []

    def open_socket(*_args):
        index = len(connections)
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connections.append(connection)
        payload = None
        reply_count = 0

        def send(message, _address):
            nonlocal payload
            payload = json.loads(message)
            sent.append((transport_clock.now, payload))

        def receive(_size):
            nonlocal reply_count
            if index == 0 and reply_count == 0:
                receiving.set()
                if not release_reply.wait(5):
                    raise RuntimeError("Test did not release the blocked response")
            reply_count += 1
            transport_clock.now += 0.2
            if (index == 0 and first_fails) or reply_count > 1:
                raise TimeoutError
            result = {"set_result": True} if payload["method"] == "ES.SetMode" else {}
            return json.dumps({"result": result}).encode(), (api.host, api.port)

        def close(*_args):
            transport_clock.now += 0.1
            closed_at.append(transport_clock.now)
            return False

        connection.sendto.side_effect = send
        connection.recvfrom.side_effect = receive
        connection.__exit__.side_effect = close
        return connection

    actions = {
        "poll": api.get_battery_status,
        "command": lambda: api.set_es_mode_passive(240),
        "discover": api.discover_devices,
    }
    with (
        patch(f"{API_MODULE}.socket.socket", side_effect=open_socket),
        ThreadPoolExecutor(max_workers=2) as workers,
    ):
        first_task = workers.submit(actions[first])
        try:
            assert receiving.wait(5)
            second_task = workers.submit(actions[second])
            assert contending.wait(5)
            assert len(connections) == 1
            assert len(sent) == 1
        finally:
            release_reply.set()
        first_result = first_task.result(timeout=5)
        second_result = second_task.result(timeout=5)

    expected_success = {"poll": {}, "command": True, "discover": [{"ip": api.host}]}
    expected_failure = {"poll": None, "command": False, "discover": []}
    assert (
        first_result == (expected_failure if first_fails else expected_success)[first]
    )
    assert second_result == expected_success[second]
    assert [payload["id"] for _, payload in sent] == [1, 2]
    assert sent[1][0] - closed_at[0] == pytest.approx(2.5)
    assert transport_clock.sleeps == [2.5]


def test_different_clients_do_not_share_a_cooldown(transport_clock):
    """One slow battery must not delay another battery's first request."""
    with patch(f"{API_MODULE}.socket.socket") as socket:
        connection = socket.return_value.__enter__.return_value
        connection.recvfrom.return_value = (b'{"result":{}}', ("192.0.2.1", 30000))
        assert MarstekAPI("192.0.2.1").get_battery_status() == {}
        assert MarstekAPI("192.0.2.2").get_battery_status() == {}
    assert transport_clock.sleeps == []
