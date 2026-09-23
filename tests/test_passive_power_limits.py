"""Passive/Manual power limits are device-aware, not a flat ±3000 W for all.

Chapter 4 of the Marstek Device Open API (Rev 3.1) documents a power rating
per model (`Set.Ver`): Venus A 1200/1500 W, Venus D 2200 W, Venus E 2500 W.
Venus C and the Venus E mini have none, so they keep the conservative
DEFAULT_MAX_PASSIVE_POWER fallback. These tests check that limit is applied
everywhere a Passive/Manual command power is produced or persisted, and that
a user's configured Max Passive Power option can only tighten it further.
"""

from __future__ import annotations

import pytest
import voluptuous as vol
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.marstek import MarstekDataUpdateCoordinator
from custom_components.marstek.capabilities import (
    KNOWN_CAPABILITIES,
    MarstekCapabilities,
    default_capabilities,
)
from custom_components.marstek.config_flow import MAX_PASSIVE_POWER_SELECTOR
from custom_components.marstek.const import (
    CONF_MAX_PASSIVE_POWER,
    DEFAULT_MAX_PASSIVE_POWER,
    DOMAIN,
    MAX_PASSIVE_POWER_LIMIT,
    PASSIVE_POWER_LIMIT_VENUS_A_W,
    PASSIVE_POWER_LIMIT_VENUS_D_W,
    PASSIVE_POWER_LIMIT_VENUS_E_W,
)
from custom_components.marstek.identity import CONF_DEVICE_INFO
from custom_components.marstek.number import MarstekPassivePowerNumber


# --------------------------------------------------------------------------
# Capability-level ranges
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected_limit"),
    [
        ("Venus A", PASSIVE_POWER_LIMIT_VENUS_A_W),
        ("Venus C", DEFAULT_MAX_PASSIVE_POWER),
        ("Venus D", PASSIVE_POWER_LIMIT_VENUS_D_W),
        ("Venus E", PASSIVE_POWER_LIMIT_VENUS_E_W),
        ("Venus E mini", DEFAULT_MAX_PASSIVE_POWER),
    ],
)
def test_known_model_hardware_limit(model, expected_limit):
    """Each known model's own capability profile carries the documented rating."""
    capabilities = next(c for c in KNOWN_CAPABILITIES if c.model == model)
    assert capabilities.passive_power_range() == (-expected_limit, expected_limit)


def test_unknown_model_uses_the_legacy_fallback():
    capabilities = default_capabilities("Something New")
    assert capabilities.passive_power_range() == (
        -DEFAULT_MAX_PASSIVE_POWER,
        DEFAULT_MAX_PASSIVE_POWER,
    )


def test_configured_limit_can_only_tighten_never_loosen():
    """A user's option cannot raise a model above its own hardware ceiling."""
    venus_a = MarstekCapabilities(
        model="Venus A",
        passive_charge_limit_w=PASSIVE_POWER_LIMIT_VENUS_A_W,
        passive_discharge_limit_w=PASSIVE_POWER_LIMIT_VENUS_A_W,
    )
    # Tighter than hardware: honored.
    assert venus_a.passive_power_range(configured_limit=800) == (-800, 800)
    # Looser than hardware: hardware wins.
    assert venus_a.passive_power_range(configured_limit=5000) == (
        -PASSIVE_POWER_LIMIT_VENUS_A_W,
        PASSIVE_POWER_LIMIT_VENUS_A_W,
    )


# --------------------------------------------------------------------------
# Coordinator: construction and capability-refresh clamping
# --------------------------------------------------------------------------


def _entry_for_model(model: str, *, max_passive_power: int | None = None) -> MockConfigEntry:
    """A config entry whose cached identity already names a model.

    Populating CONF_DEVICE_INFO lets the coordinator resolve real capabilities
    immediately at construction, without a network round trip.
    """
    data = {
        "host": "192.0.2.1",
        "port": 30000,
        CONF_DEVICE_INFO: {"device": model, "ble_mac": "AA:BB:CC:DD:EE:01"},
    }
    if max_passive_power is not None:
        data[CONF_MAX_PASSIVE_POWER] = max_passive_power
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Marstek {model}",
        unique_id="AA:BB:CC:DD:EE:01",
        data=data,
    )


@pytest.mark.asyncio
async def test_coordinator_clamps_calibration_to_known_model_limit(
    hass, mock_marstek_api
):
    """A Venus A entry left at the default option is still capped at 1500 W."""
    entry = _entry_for_model("Venus A")
    entry.add_to_hass(hass)
    coordinator = MarstekDataUpdateCoordinator(
        hass,
        mock_marstek_api,
        entry,
        max_passive_power=DEFAULT_MAX_PASSIVE_POWER,
    )
    assert coordinator.calibration.command_max == PASSIVE_POWER_LIMIT_VENUS_A_W
    assert coordinator.calibration.command_min == -PASSIVE_POWER_LIMIT_VENUS_A_W


@pytest.mark.asyncio
async def test_coordinator_uses_the_tighter_of_configured_and_hardware_limit(
    hass, mock_marstek_api
):
    """A configured ceiling below the hardware rating wins when it is tighter."""
    entry = _entry_for_model("Venus D", max_passive_power=1000)
    entry.add_to_hass(hass)
    coordinator = MarstekDataUpdateCoordinator(
        hass, mock_marstek_api, entry, max_passive_power=1000
    )
    assert coordinator.calibration.command_max == 1000
    assert coordinator.calibration.command_min == -1000


@pytest.mark.asyncio
async def test_coordinator_recomputes_range_once_model_is_confirmed(
    hass, marstek_entry, mock_marstek_api
):
    """A cached-unknown guess widens/narrows correctly once the model is live-confirmed.

    `marstek_entry` has no cached CONF_DEVICE_INFO, so construction resolves the
    baseline (legacy fallback) profile; only a real poll confirms Venus A.
    """
    marstek_entry.add_to_hass(hass)
    mock_marstek_api.get_device_info.return_value = {
        "device": "Venus A",
        "ble_mac": "AA:BB:CC:DD:EE:01",
    }
    coordinator = MarstekDataUpdateCoordinator(hass, mock_marstek_api, marstek_entry)
    assert coordinator.calibration.command_max == DEFAULT_MAX_PASSIVE_POWER

    await coordinator._async_update_data()

    assert coordinator.capabilities.model == "Venus A"
    assert coordinator.calibration.command_max == PASSIVE_POWER_LIMIT_VENUS_A_W
    assert coordinator.calibration.command_min == -PASSIVE_POWER_LIMIT_VENUS_A_W


@pytest.mark.asyncio
async def test_old_oversized_calibration_is_clamped_when_model_is_known(
    hass, mock_marstek_api
):
    """A calibration saved before the tighter limit was known loads clamped."""
    entry = _entry_for_model("Venus A")
    entry.add_to_hass(hass)
    oversized = {
        "version": 1,
        "bucket_width": 20,
        "discharge": {"1500": 2900.0},
        "charge": {},
    }
    coordinator = MarstekDataUpdateCoordinator(
        hass,
        mock_marstek_api,
        entry,
        calibration_data=oversized,
        max_passive_power=DEFAULT_MAX_PASSIVE_POWER,
    )
    command, _source = coordinator.calibration.command_for(1500)
    assert command == PASSIVE_POWER_LIMIT_VENUS_A_W


# --------------------------------------------------------------------------
# Consumers: async_set_passive_power, the number entity
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_set_passive_power_cannot_exceed_the_model_limit(
    hass, mock_marstek_api
):
    """A request beyond the model's own ceiling is clamped, not honored."""
    entry = _entry_for_model("Venus A")
    entry.add_to_hass(hass)
    coordinator = MarstekDataUpdateCoordinator(
        hass, mock_marstek_api, entry, max_passive_power=DEFAULT_MAX_PASSIVE_POWER
    )
    try:
        await coordinator.async_set_passive_power(3000)
        assert coordinator.desired_power == PASSIVE_POWER_LIMIT_VENUS_A_W
        assert coordinator.command_power <= PASSIVE_POWER_LIMIT_VENUS_A_W
        mock_marstek_api.set_es_mode_passive.assert_called_once_with(
            PASSIVE_POWER_LIMIT_VENUS_A_W
        )
    finally:
        await coordinator.async_stop_passive_control()


@pytest.mark.asyncio
async def test_number_entity_bounds_follow_the_model_limit(hass, mock_marstek_api):
    """The Passive Power number's min/max mirror the resolved model's ceiling."""
    entry = _entry_for_model("Venus E")
    entry.add_to_hass(hass)
    coordinator = MarstekDataUpdateCoordinator(
        hass, mock_marstek_api, entry, max_passive_power=DEFAULT_MAX_PASSIVE_POWER
    )
    number = MarstekPassivePowerNumber(coordinator)
    assert number.native_max_value == PASSIVE_POWER_LIMIT_VENUS_E_W
    assert number.native_min_value == -PASSIVE_POWER_LIMIT_VENUS_E_W


# --------------------------------------------------------------------------
# Configuration/options UI ceiling
# --------------------------------------------------------------------------


def test_config_flow_selector_no_longer_allows_30000():
    """The old unsafe 30000 W ceiling is gone from both the constant and schema."""
    assert MAX_PASSIVE_POWER_LIMIT == DEFAULT_MAX_PASSIVE_POWER
    with pytest.raises(vol.Invalid):
        MAX_PASSIVE_POWER_SELECTOR(30000)


def test_config_flow_selector_allows_up_to_the_legacy_fallback():
    assert MAX_PASSIVE_POWER_SELECTOR(DEFAULT_MAX_PASSIVE_POWER) == (
        DEFAULT_MAX_PASSIVE_POWER
    )
