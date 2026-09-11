"""Pytest configuration for the Marstek integration tests."""

from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.marstek.const import DOMAIN
from custom_components.marstek.marstek_api import MarstekAPI

pytest_plugins = ("pytest_homeassistant_custom_component",)


@pytest.fixture
def marstek_entry():
    """An existing config entry, with the original API spelling of its MAC."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Marstek Venus A",
        unique_id="AA:BB:CC:DD:EE:01",
        data={"host": "192.0.2.1", "port": 30000},
    )


@pytest.fixture
def mock_marstek_api():
    """Mock UDP calls while exercising the real setup and registry lifecycle."""
    api = MagicMock(spec=MarstekAPI)
    api.get_device_info.return_value = {
        "device": "Venus A",
        "ble_mac": "AA:BB:CC:DD:EE:01",
        "ver": 123,
    }
    for method in (
        "get_wifi_status",
        "get_ble_status",
        "get_pv_status",
        "get_es_status",
        "get_em_status",
    ):
        getattr(api, method).return_value = {}
    api.get_battery_status.return_value = {"soc": 50}
    api.get_es_mode.return_value = {"mode": "Auto", "ongrid_power": 0}
    api.set_es_mode_passive.return_value = True
    api.set_es_mode_auto.return_value = True
    with (
        patch("custom_components.marstek.MarstekAPI", return_value=api),
        patch("custom_components.marstek.config_flow.MarstekAPI", return_value=api),
    ):
        yield api
