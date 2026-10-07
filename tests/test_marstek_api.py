"""Verify the existing wire acknowledgement semantics and failure diagnostics."""

import json
import logging
import socket
from unittest.mock import patch

import pytest

from custom_components.marstek import MarstekDataUpdateCoordinator
from custom_components.marstek.marstek_api import MarstekAPI

API_MODULE = "custom_components.marstek.marstek_api"
DISCOVERY_DEVICE = {
    "device": "Venus A",
    "ble_mac": "AA:BB:CC:DD:EE:01",
    "ip": "192.0.2.1",
}


@pytest.mark.parametrize("log_level", [logging.DEBUG, logging.INFO])
@pytest.mark.parametrize(
    ("api_method", "args", "method", "params", "destination"),
    [
        ("get_es_status", (), "ES.GetStatus", {"id": 0}, "192.0.2.1"),
        ("get_es_mode", (), "ES.GetMode", {"id": 0}, "192.0.2.1"),
        ("get_battery_status", (), "Bat.GetStatus", {"id": 0}, "192.0.2.1"),
        ("get_pv_status", (), "PV.GetStatus", {"id": 0}, "192.0.2.1"),
        ("get_wifi_status", (), "Wifi.GetStatus", {"id": 0}, "192.0.2.1"),
        ("get_ble_status", (), "BLE.GetStatus", {"id": 0}, "192.0.2.1"),
        ("get_em_status", (), "EM.GetStatus", {"id": 0}, "192.0.2.1"),
        ("get_device_info", (), "Marstek.GetDevice", {"ble_mac": "0"}, "192.0.2.1"),
        (
            "_send_request",
            ("Future.GetStatus",),
            "Future.GetStatus",
            {"id": 0},
            "192.0.2.1",
        ),
        (
            "set_es_mode_passive",
            (-827,),
            "ES.SetMode",
            {
                "id": 0,
                "config": {
                    "mode": "Passive",
                    "passive_cfg": {"power": -827, "cd_time": 3600},
                },
            },
            "192.0.2.1",
        ),
        (
            "discover_devices",
            (),
            "Marstek.GetDevice",
            {"ble_mac": "0"},
            "255.255.255.255",
        ),
        (
            "discover_devices",
            ("192.0.2.255",),
            "Marstek.GetDevice",
            {"ble_mac": "0"},
            "192.0.2.255",
        ),
    ],
)
def test_tx_logs_wire_payload_before_send(
    caplog, log_level, api_method, args, method, params, destination
):
    """Reads, writes and discovery log exact requests once, only at DEBUG."""
    api = MarstekAPI("venus-a.local", port=30001)
    api._request_id = 122
    caplog.set_level(log_level, logger=API_MODULE)
    expected = {"id": 123, "method": method, "params": params}
    discovery = api_method == "discover_devices"
    host = destination if discovery else api.host

    def send(payload, address):
        sent = json.loads(payload)
        records = [r for r in caplog.records if r.message.startswith("Marstek TX:")]
        if log_level == logging.DEBUG:
            assert len(records) == 1
            assert records[0].levelno == logging.DEBUG
            assert records[0].message == (
                f"Marstek TX: host={host} ip={destination} port=30001 "
                f"request_id={sent['id']} method={method} params={params}"
            )
        else:
            assert records == []
        return len(payload)

    with (
        patch(f"{API_MODULE}.socket.gethostbyname", return_value="192.0.2.1"),
        patch(f"{API_MODULE}.socket.socket") as socket_factory,
    ):
        connection = socket_factory.return_value
        connection.__enter__.return_value = connection
        connection.sendto.side_effect = send
        reply = DISCOVERY_DEVICE if discovery else {"set_result": True}
        connection.recvfrom.side_effect = [
            (
                json.dumps({"id": 123, "result": reply}).encode(),
                ("192.0.2.1", api.port),
            ),
            TimeoutError(),
        ]
        result = getattr(api, api_method)(*args)
        if discovery:
            assert result == [DISCOVERY_DEVICE]
        elif method == "ES.SetMode":
            assert result is True
        else:
            assert result == {"set_result": True}
        connection.sendto.assert_called_once_with(
            json.dumps(expected).encode("utf-8"), (destination, api.port)
        )
    records = [r for r in caplog.records if r.message.startswith("Marstek TX:")]
    assert len(records) == (1 if log_level == logging.DEBUG else 0)


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
        connection = socket_factory.return_value
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
        ({"src": "venus", "result": {"id": 0, "set_result": True}}, True),
        ({"src": None, "extra": 42, "result": {"set_result": True}}, True),
        ({"result": {"set_result": 1}}, False),
        ({"result": {"set_result": 1.0}}, False),
        ({"result": {"set_result": "true"}}, False),
        ({"result": {"set_result": None}}, False),
        ({"result": {"set_result": False}}, False),
        ({"result": {}}, False),
        ({"result": None}, False),
        ({"result": []}, False),
        ({"result": [{"set_result": True}]}, False),
        ({"result": "success"}, False),
        ({"result": True}, False),
        ({"result": 1}, False),
        ({}, False),
        ({"error": {"code": -1, "message": "rejected"}}, False),
    ],
)
def test_mode_success_requires_set_result(mode, args, config, response, success):
    """Transport success alone never acknowledges an operating-mode command."""
    api = MarstekAPI("192.0.2.1")
    with patch("custom_components.marstek.marstek_api.socket.socket") as socket:
        connection = socket.return_value
        connection.recvfrom.return_value = (
            json.dumps({"id": 1, **response}).encode(),
            ("192.0.2.1", 30000),
        )
        assert getattr(api, f"set_es_mode_{mode}")(*args) is success
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
        (b'{"id":1,"result":{"set_result":1}}', None),
        (b'{"id":1,"result":{}}', None),
        (b'{"id":1,"result":[]}', None),
        (b'{"id":1,"result":null}', None),
        (b'{"id":1}', None),
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
        connection = socket.return_value
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
        (b'{"id":0,"result":{}}', "192.0.2.1", "malformed response with id=0"),
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
            "malformed response with id=0",
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
        connection = socket.return_value
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
        connection = socket.return_value
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
        record for record in caplog.records if record.message.startswith("Ignored")
    ]
    assert ignored
    assert all(record.levelno == logging.DEBUG for record in ignored)


@pytest.mark.parametrize(
    "packet",
    [
        b'{"id":0,"result":{"set_result":false}}',
        b'{"id":0,"result":{"id":1,"set_result":true}}',
        b'{"id":2,"result":{"set_result":true}}',
        b'{"id":0,"error":{"code":-32700,"message":"Parse error","data":403}}',
    ],
)
@pytest.mark.parametrize("matching_ack", [False, True])
@pytest.mark.asyncio
async def test_mode_command_ignores_mismatching_responses(
    hass, marstek_entry, caplog, packet, matching_ack
):
    """Only a matching acknowledgement succeeds; unrelated packets are ignored."""
    marstek_entry.add_to_hass(hass)
    coordinator = MarstekDataUpdateCoordinator(
        hass, MarstekAPI("192.0.2.1"), marstek_entry
    )
    with (
        patch(f"{API_MODULE}.socket.socket") as socket,
        patch("custom_components.marstek.async_call_later") as later,
    ):
        connection = socket.return_value
        connection.recvfrom.side_effect = [
            (packet, ("192.0.2.1", 30000)),
            (
                b'{"id":1,"result":{"id":0,"set_result":true}}',
                ("192.0.2.1", 30000),
            )
            if matching_ack
            else TimeoutError(),
        ]
        assert await coordinator.async_set_passive_power(240) is matching_ack
        assert later.call_args.args[1] == (180 if matching_ack else 15)
        assert connection.recvfrom.call_count == 2
        connection.sendto.assert_called_once()
        warnings = [
            record for record in caplog.records if record.levelno >= logging.WARNING
        ]
        assert len(warnings) == (0 if matching_ack else 1)
        await coordinator.async_stop_passive_control()


def discovery_packet(result=None, request_id=1):
    """Encode a discovery reply with an outer request ID."""
    return json.dumps(
        {
            "id": request_id,
            "result": DISCOVERY_DEVICE if result is None else result,
        }
    ).encode()


@pytest.mark.parametrize("request_id", [0, 1, 123])
@pytest.mark.parametrize(
    "optional_fields",
    [
        {},
        {
            "id": 0,
            "ver": 123,
            "wifi_mac": "AA:BB:CC:DD:EE:02",
            "wifi_name": "LAN",
        },
    ],
)
def test_discovery_accepts_valid_reply(request_id, optional_fields):
    """Discovery echoes the caller's outer ID; firmware metadata is optional."""
    api = MarstekAPI("255.255.255.255")
    api._request_id = request_id - 1
    device = {**DISCOVERY_DEVICE, **optional_fields}
    with patch(f"{API_MODULE}.socket.socket") as socket_factory:
        connection = socket_factory.return_value.__enter__.return_value
        connection.recvfrom.side_effect = [
            (discovery_packet(device, request_id), (device["ip"], 30001)),
            TimeoutError(),
        ]
        assert api.discover_devices() == [device]
        connection.setsockopt.assert_called_once_with(
            socket.SOL_SOCKET, socket.SO_BROADCAST, 1
        )
        payload, destination = connection.sendto.call_args.args
        assert json.loads(payload) == {
            "id": request_id,
            "method": "Marstek.GetDevice",
            "params": {"ble_mac": "0"},
        }
        assert destination == ("255.255.255.255", 30000)
        assert connection.recvfrom.call_count == 2


@pytest.mark.parametrize(
    "duplicate_mac",
    [
        "AA:BB:CC:DD:EE:01",
        "aa:bb:cc:dd:ee:01",
        "aabbccddee01",
        "AA-BB-CC-DD-EE-01",
    ],
)
def test_discovery_multiple_devices_and_duplicates(duplicate_mac, caplog):
    """Identity deduplication works across spelling variants and source IPs."""
    api = MarstekAPI("255.255.255.255")
    second = {**DISCOVERY_DEVICE, "ble_mac": "AA:BB:CC:DD:EE:02", "ip": "192.0.2.2"}
    duplicate = {**DISCOVERY_DEVICE, "ble_mac": duplicate_mac, "ip": "192.0.2.3"}
    caplog.set_level(logging.DEBUG, logger=API_MODULE)
    with patch(f"{API_MODULE}.socket.socket") as socket_factory:
        connection = socket_factory.return_value.__enter__.return_value
        connection.recvfrom.side_effect = [
            (discovery_packet(), ("192.0.2.1", 30000)),
            (discovery_packet(), ("192.0.2.1", 30000)),
            (discovery_packet(duplicate), ("192.0.2.3", 30000)),
            (discovery_packet(second), ("192.0.2.2", 30000)),
            TimeoutError(),
        ]
        assert api.discover_devices() == [DISCOVERY_DEVICE, second]
        assert connection.recvfrom.call_count == 5
        connection.sendto.assert_called_once()
    assert "duplicate device" in caplog.text


@pytest.mark.parametrize(
    ("packet", "diagnostic"),
    [
        (b"invalid json", "invalid JSON"),
        (b"\xff", "invalid JSON"),
        pytest.param(b"[" * 1100, "invalid JSON", id="deeply-nested-json"),
        pytest.param(
            b'{"id":' + b"1" * 4500 + b"}", "invalid JSON", id="oversized-integer"
        ),
        (b"[]", "non-object response"),
        (b'"unrelated"', "non-object response"),
        (b"123", "non-object response"),
        (b"null", "non-object response"),
        (b"true", "non-object response"),
        (json.dumps({"result": DISCOVERY_DEVICE}).encode(), "missing request ID"),
        (discovery_packet(request_id=99), "mismatching request ID"),
        (discovery_packet(request_id="0"), "invalid request ID type"),
        (discovery_packet(request_id="1"), "invalid request ID type"),
        (discovery_packet(request_id=True), "invalid request ID type"),
        (discovery_packet(request_id=1.0), "invalid request ID type"),
        (discovery_packet(request_id=None), "invalid request ID type"),
        (discovery_packet(request_id=[]), "invalid request ID type"),
        (discovery_packet(request_id={}), "invalid request ID type"),
        (b'{"id":1}', "missing/invalid result"),
        (b'{"id":1,"result":null}', "missing/invalid result"),
        (b'{"id":1,"result":[]}', "missing/invalid result"),
        (b'{"id":1,"result":"device"}', "missing/invalid result"),
        (b'{"id":1,"result":42}', "missing/invalid result"),
        (b'{"id":1,"result":true}', "missing/invalid result"),
        (
            b'{"jsonrpc":"2.0","id":1,"error":{"code":-1,"message":"rejected"}}',
            "API error response: -1 - rejected",
        ),
        (b'{"id":99,"error":{"code":-1,"message":"stale"}}', "mismatching request ID"),
        (b'{"id":1,"error":null}', "invalid API error response"),
        (b'{"id":1,"error":[]}', "invalid API error response"),
        (b'{"id":1,"error":{}}', "invalid API error response"),
        (
            json.dumps(
                {
                    "id": 1,
                    "error": {"code": -1, "message": "rejected"},
                    "result": DISCOVERY_DEVICE,
                }
            ).encode(),
            "API error response",
        ),
        (b'{"method":"unrelated","params":{"id":0}}', "missing request ID"),
        (b'{"id":1,"result":{"set_result":true}}', "required device fields"),
    ],
)
def test_discovery_ignores_bad_packet_and_keeps_devices(caplog, packet, diagnostic):
    """One malformed, unrelated or error reply cannot abort broadcast discovery."""
    api = MarstekAPI("255.255.255.255")
    second = {**DISCOVERY_DEVICE, "ble_mac": "AA:BB:CC:DD:EE:02", "ip": "192.0.2.2"}
    caplog.set_level(logging.DEBUG, logger=API_MODULE)
    with patch(f"{API_MODULE}.socket.socket") as socket_factory:
        connection = socket_factory.return_value.__enter__.return_value
        connection.recvfrom.side_effect = [
            (discovery_packet(), ("192.0.2.1", 30000)),
            (packet, ("192.0.2.99", 45678)),
            (discovery_packet(second), ("192.0.2.2", 30000)),
            TimeoutError(),
        ]
        assert api.discover_devices() == [DISCOVERY_DEVICE, second]
        assert connection.recvfrom.call_count == 4
        connection.sendto.assert_called_once()
    assert diagnostic in caplog.text
    assert all(record.levelno == logging.DEBUG for record in caplog.records)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("device", None),
        ("device", ""),
        ("device", "   "),
        ("device", 42),
        ("ble_mac", None),
        ("ble_mac", []),
        ("ble_mac", "unknown"),
        ("ble_mac", "00:00:00:00:00:00"),
        ("ble_mac", "FF:FF:FF:FF:FF:FF"),
        ("ip", None),
        ("ip", ""),
        ("ip", 123),
        ("ip", "not-an-ip"),
        ("ip", "192.0.2.999"),
        ("ip", "::1"),
        ("ip", "0.0.0.0"),
        ("ip", "255.255.255.255"),
        ("ip", "224.0.0.1"),
    ],
)
def test_discovery_rejects_invalid_required_fields(field, value, caplog):
    """Unusable device identities and network addresses are isolated to one reply."""
    invalid_device = {**DISCOVERY_DEVICE, field: value}
    _assert_discovery_rejects_result(invalid_device, caplog)


@pytest.mark.parametrize("field", ["device", "ble_mac", "ip"])
def test_discovery_rejects_missing_required_fields(field, caplog):
    """Each required field must be present even when the UDP sender is usable."""
    invalid_device = {
        key: value for key, value in DISCOVERY_DEVICE.items() if key != field
    }
    _assert_discovery_rejects_result(invalid_device, caplog)


def _assert_discovery_rejects_result(result, caplog):
    api = MarstekAPI("255.255.255.255")
    caplog.set_level(logging.DEBUG, logger=API_MODULE)
    with patch(f"{API_MODULE}.socket.socket") as socket_factory:
        connection = socket_factory.return_value.__enter__.return_value
        connection.recvfrom.side_effect = [
            (discovery_packet(result), ("192.0.2.1", 30000)),
            (discovery_packet(), ("192.0.2.1", 30000)),
            TimeoutError(),
        ]
        assert api.discover_devices() == [DISCOVERY_DEVICE]
        assert connection.recvfrom.call_count == 3
    assert "Ignored discovery packet" in caplog.text
    assert all(record.levelno == logging.DEBUG for record in caplog.records)


def test_discovery_uses_sender_instead_of_stale_reported_ip(caplog):
    """A sane but stale firmware address must not redirect subsequent connections."""
    api = MarstekAPI("255.255.255.255")
    caplog.set_level(logging.DEBUG, logger=API_MODULE)
    with patch(f"{API_MODULE}.socket.socket") as socket_factory:
        connection = socket_factory.return_value.__enter__.return_value
        connection.recvfrom.side_effect = [
            (discovery_packet(), ("192.0.2.2", 45678)),
            TimeoutError(),
        ]
        assert api.discover_devices() == [{**DISCOVERY_DEVICE, "ip": "192.0.2.2"}]
    assert "using sender" in caplog.text


@pytest.mark.parametrize(
    "sender", ["0.0.0.0", "224.0.0.1", "255.255.255.255", "bad-ip"]
)
def test_discovery_rejects_invalid_sender(sender):
    """An unusable network source cannot create a device entry."""
    with patch(f"{API_MODULE}.socket.socket") as socket_factory:
        connection = socket_factory.return_value.__enter__.return_value
        connection.recvfrom.side_effect = [
            (discovery_packet(), (sender, 30000)),
            (discovery_packet(), ("192.0.2.1", 30000)),
            TimeoutError(),
        ]
        assert MarstekAPI("255.255.255.255").discover_devices() == [DISCOVERY_DEVICE]
        assert connection.recvfrom.call_count == 3


@pytest.mark.parametrize("valid_reply", [False, True])
@pytest.mark.parametrize("continuous_noise", [False, True])
def test_discovery_packets_do_not_extend_deadline(
    valid_reply, continuous_noise, caplog
):
    """A quiet timeout or continuous noise returns devices collected so far."""
    api = MarstekAPI("255.255.255.255", timeout=5.0)
    now = 0.0
    received = 0

    def receive(_size):
        nonlocal now, received
        received += 1
        if received == 1:
            now = 2.0
            return (discovery_packet() if valid_reply else b"noise"), (
                "192.0.2.1",
                30000,
            )
        now = 5.0
        if continuous_noise:
            return b"noise", ("192.0.2.99", 30000)
        raise TimeoutError

    caplog.set_level(logging.DEBUG, logger=API_MODULE)
    with (
        patch(f"{API_MODULE}.monotonic", side_effect=lambda: now),
        patch(f"{API_MODULE}.socket.socket") as socket_factory,
    ):
        connection = socket_factory.return_value.__enter__.return_value
        connection.recvfrom.side_effect = receive
        assert api.discover_devices() == ([DISCOVERY_DEVICE] if valid_reply else [])
        assert received == 2
        assert [call.args[0] for call in connection.settimeout.call_args_list] == [
            5,
            5,
            3,
        ]
        connection.sendto.assert_called_once()
        assert api._next_request_at == 7.5
    assert all(record.levelno == logging.DEBUG for record in caplog.records)


def test_discovery_timeout_without_replies(caplog):
    """No responders still means an empty discovery result without error logs."""
    with patch(f"{API_MODULE}.socket.socket") as socket_factory:
        connection = socket_factory.return_value.__enter__.return_value
        connection.recvfrom.side_effect = TimeoutError
        assert MarstekAPI("255.255.255.255").discover_devices() == []
        assert connection.recvfrom.call_count == 1
    assert all(record.levelno == logging.DEBUG for record in caplog.records)
