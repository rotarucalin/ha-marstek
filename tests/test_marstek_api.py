"""Verify the existing wire acknowledgement semantics and failure diagnostics."""

import json
import logging
from unittest.mock import patch

import pytest

from custom_components.marstek import MarstekDataUpdateCoordinator
from custom_components.marstek.marstek_api import MarstekAPI


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
            json.dumps(response).encode(),
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
        (b"invalid json", "Failed to decode JSON response"),
        (b'{"error":{"code":-1,"message":"rejected"}}', "API error: -1 - rejected"),
        (b'{"result":{"set_result":false}}', None),
        (b'{"result":{}}', None),
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
            connection.recvfrom.return_value = (failure, ("192.0.2.1", 30000))
        assert not await coordinator.async_set_passive_power(240)
        assert later.call_args.args[1] == 15
        assert coordinator._passive_power_target == 240
        assert "Marstek command succeeded:" not in caplog.text
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert "source=new_target" in warnings[0].message
        assert "retry in 15s" in warnings[0].message
        if detail:
            assert detail in caplog.text
            assert "host=192.0.2.1 port=30000 method=ES.SetMode" in caplog.text
        await coordinator.async_stop_passive_control()
