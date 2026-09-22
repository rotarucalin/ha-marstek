"""Verify the existing wire acknowledgement semantics and failure diagnostics."""

import json
import logging
import socket
from unittest.mock import patch

import pytest

from custom_components.marstek import MarstekDataUpdateCoordinator
from custom_components.marstek.marstek_api import MarstekAPI

API_MODULE = "custom_components.marstek.marstek_api"


@pytest.mark.parametrize("host", ["192.0.2.1", "venus-a.local"])
def test_request_validates_resolved_sender(host):
    """Literal IPv4 addresses and hostnames accept the resolved device's reply."""
    api = MarstekAPI(host)
    with (
        patch(
            f"{API_MODULE}.socket.gethostbyname", return_value="192.0.2.1"
        ) as resolve,
        patch(f"{API_MODULE}.socket.socket") as socket_factory,
    ):
        connection = socket_factory.return_value.__enter__.return_value
        connection.recvfrom.side_effect = [
            (b'{"id":1,"result":{"soc":50}}', ("192.0.2.1", 30000)),
            TimeoutError(),
        ]
        assert api.get_battery_status() == {"soc": 50}
        resolve.assert_called_once_with(host)
        assert connection.sendto.call_args.args[1] == ("192.0.2.1", 30000)
        assert connection.recvfrom.call_count == 1


def test_hostname_resolution_failure(caplog):
    """A DNS failure returns no result and logs context without sending a request."""
    api = MarstekAPI("venus-a.local")
    with (
        patch(
            f"{API_MODULE}.socket.gethostbyname",
            side_effect=socket.gaierror(socket.EAI_NONAME, "Name resolution failed"),
        ) as resolve,
        patch(f"{API_MODULE}.socket.socket") as socket_factory,
    ):
        assert api.get_battery_status() is None
        resolve.assert_called_once_with(api.host)
        socket_factory.assert_not_called()
    assert "host=venus-a.local port=30000 method=Bat.GetStatus" in caplog.text
    assert "gaierror" in caplog.text
    assert "Name resolution failed" in caplog.text


@pytest.mark.parametrize(
    ("mode", "args", "config"),
    [
        ("auto", (), {"mode": "Auto", "auto_cfg": {"enable": 1}}),
        ("ai", (), {"mode": "AI", "ai_cfg": {"enable": 1}}),
        (
            "passive",
            (240,),
            {"mode": "Passive", "passive_cfg": {"power": 240, "cd_time": 3600}},
        ),
        (
            "manual",
            (0, "00:00", "23:59", 127, 100, 1),
            {
                "mode": "Manual",
                "manual_cfg": {
                    "time_num": 0,
                    "start_time": "00:00",
                    "end_time": "23:59",
                    "week_set": 127,
                    "power": 100,
                    "enable": 1,
                },
            },
        ),
    ],
)
@pytest.mark.parametrize(
    ("response", "success"),
    [
        ({"result": {"set_result": True}}, True),
        ({"result": {"set_result": 1}}, True),
        ({"result": {"set_result": False}}, False),
        ({"result": {}}, False),
        ({"result": None}, False),
        ({}, False),
        ({"error": {"code": -1, "message": "rejected"}}, False),
    ],
)
def test_mode_success_requires_set_result(mode, args, config, response, success):
    """Transport success alone never acknowledges an operating-mode command."""
    api = MarstekAPI("192.0.2.1")
    with patch("custom_components.marstek.marstek_api.socket.socket") as socket:
        connection = socket.return_value.__enter__.return_value
        connection.recvfrom.return_value = (
            json.dumps({"id": 1, **response}).encode(),
            ("192.0.2.1", 30000),
        )
        assert bool(getattr(api, f"set_es_mode_{mode}")(*args)) is success
        payload, address = connection.sendto.call_args.args
        assert address == ("192.0.2.1", 30000)
        assert json.loads(payload) == {
            "id": 1,
            "method": "ES.SetMode",
            "params": {"id": 0, "config": config},
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "detail"),
    [
        (TimeoutError(), "Timeout communicating with device"),
        (OSError("offline"), "OSError: offline"),
        (b"invalid json", "Ignored malformed packet"),
        (
            b'{"id":1,"error":{"code":-1,"message":"rejected"}}',
            "API error: -1 - rejected",
        ),
        (b'{"id":1,"result":{"set_result":false}}', None),
        (b'{"id":1,"result":{}}', None),
    ],
)
async def test_real_api_failure_schedules_retry_with_one_warning(
    hass, marstek_entry, caplog, failure, detail
):
    """Wire errors add DEBUG detail; one coordinator warning explains recovery."""
    marstek_entry.add_to_hass(hass)
    coordinator = MarstekDataUpdateCoordinator(
        hass, MarstekAPI("192.0.2.1"), marstek_entry
    )
    caplog.set_level(logging.DEBUG, logger="custom_components.marstek")
    with (
        patch("custom_components.marstek.marstek_api.socket.socket") as socket,
        patch("custom_components.marstek.async_call_later") as later,
    ):
        connection = socket.return_value.__enter__.return_value
        if isinstance(failure, Exception):
            connection.recvfrom.side_effect = failure
        else:
            connection.recvfrom.side_effect = [
                (failure, ("192.0.2.1", 30000)),
                TimeoutError(),
            ]
        assert not await coordinator.async_set_passive_power(240)
        assert later.call_args.args[1] == 15
        assert coordinator.desired_power == 240
        assert coordinator.command_power == 240
        assert "Marstek command succeeded:" not in caplog.text
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert "source=new_target" in warnings[0].message
        assert "retry in 15s" in warnings[0].message
        if detail:
            assert detail in caplog.text
            assert "host=192.0.2.1 port=30000 method=ES.SetMode" in caplog.text
        await coordinator.async_stop_passive_control()


@pytest.mark.parametrize(
    ("packet", "sender", "diagnostic"),
    [
        (b'{"id":0,"result":{}}', "192.0.2.1", "mismatching request ID"),
        (b'{"id":1,"result":{}}', "192.0.2.2", "unexpected sender"),
        (b"invalid json", "192.0.2.1", "malformed packet"),
        (b"\xff", "192.0.2.1", "malformed packet"),
        (b"[]", "192.0.2.1", "expected JSON object"),
        (b"null", "192.0.2.1", "expected JSON object"),
        (b'{"result":{"id":1}}', "192.0.2.1", "response_id=None"),
        (b'{"id":null,"result":{}}', "192.0.2.1", "response_id=None"),
        (b'{"id":true,"result":{}}', "192.0.2.1", "response_id=True"),
        (b'{"id":1.0,"result":{}}', "192.0.2.1", "response_id=1.0"),
        (b'{"id":"1","result":{}}', "192.0.2.1", "response_id='1'"),
        (b'{"id":[],"result":{}}', "192.0.2.1", "response_id=[]"),
        (b'{"id":{},"result":{}}', "192.0.2.1", "response_id={}"),
        (
            b'{"id":0,"error":{"code":-1,"message":"stale"}}',
            "192.0.2.1",
            "mismatching request ID",
        ),
    ],
)
@pytest.mark.parametrize("host", ["192.0.2.1", "venus-a.local"])
def test_invalid_packet_followed_by_matching_response(
    caplog, packet, sender, diagnostic, host
):
    """An unrelated first packet must not fail or resend the current request."""
    api = MarstekAPI(host)
    caplog.set_level(logging.DEBUG, logger=API_MODULE)
    with (
        patch(
            f"{API_MODULE}.socket.gethostbyname", return_value="192.0.2.1"
        ) as resolve,
        patch(f"{API_MODULE}.socket.socket") as socket,
    ):
        connection = socket.return_value.__enter__.return_value
        connection.recvfrom.side_effect = [
            (packet, (sender, 30000)),
            (b'{"id":1,"result":{"soc":50}}', ("192.0.2.1", api.port)),
        ]
        assert api.get_battery_status() == {"soc": 50}
        assert connection.recvfrom.call_count == 2
        connection.sendto.assert_called_once()
        resolve.assert_called_once_with(host)
    assert diagnostic in caplog.text
    assert f"host={host}" in caplog.text
    assert "request_id=1" in caplog.text
    assert all(record.levelno == logging.DEBUG for record in caplog.records)


@pytest.mark.parametrize("packets_keep_arriving", [False, True])
def test_invalid_packets_do_not_reset_timeout(caplog, packets_keep_arriving):
    """Both a quiet socket and continuous invalid traffic exhaust one deadline."""
    api = MarstekAPI("192.0.2.1", timeout=5.0)
    now = 0.0
    received = 0

    def receive(_size):
        nonlocal now, received
        received += 1
        if received == 1:
            now = 2.0
            return b'{"id":0,"result":{}}', (api.host, api.port)
        now = 5.0
        if packets_keep_arriving:
            return b'{"result":{}}', (api.host, api.port)
        raise TimeoutError

    caplog.set_level(logging.DEBUG, logger=API_MODULE)
    with (
        patch(f"{API_MODULE}.monotonic", side_effect=lambda: now),
        patch(f"{API_MODULE}.socket.socket") as socket,
    ):
        connection = socket.return_value.__enter__.return_value
        connection.recvfrom.side_effect = receive
        assert api.get_battery_status() is None
        assert received == 2
        connection.sendto.assert_called_once()
        assert [call.args[0] for call in connection.settimeout.call_args_list] == [
            5.0,
            5.0,
            3.0,
        ]
        assert api._next_request_at == 7.5

    assert "Timeout communicating with device" in caplog.text
    ignored = [
        record for record in caplog.records if "Ignored packet" in record.message
    ]
    assert ignored
    assert all(record.levelno == logging.DEBUG for record in ignored)


@pytest.mark.asyncio
async def test_invalid_packet_then_valid_ack_does_not_schedule_retry(
    hass, marstek_entry, caplog
):
    """A stale acknowledgement must not trigger the coordinator's failure retry."""
    marstek_entry.add_to_hass(hass)
    coordinator = MarstekDataUpdateCoordinator(
        hass, MarstekAPI("192.0.2.1"), marstek_entry
    )
    with (
        patch(f"{API_MODULE}.socket.socket") as socket,
        patch("custom_components.marstek.async_call_later") as later,
    ):
        connection = socket.return_value.__enter__.return_value
        connection.recvfrom.side_effect = [
            (b'{"id":0,"result":{"set_result":false}}', ("192.0.2.1", 30000)),
            (b'{"id":1,"result":{"set_result":true}}', ("192.0.2.1", 30000)),
        ]
        assert await coordinator.async_set_passive_power(240)
        assert later.call_args.args[1] == 180
        connection.sendto.assert_called_once()
        assert not [
            record for record in caplog.records if record.levelno >= logging.WARNING
        ]
        await coordinator.async_stop_passive_control()
