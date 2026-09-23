"""Model capabilities drive PV, mode and Manual behaviour for every model."""

import json
import logging
from dataclasses import FrozenInstanceError
from unittest.mock import patch

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.marstek import MarstekDataUpdateCoordinator
from custom_components.marstek.capabilities import (
    ALL_ES_MODES,
    KNOWN_CAPABILITIES,
    MarstekCapabilities,
    default_capabilities,
    normalize_model,
    reset_unknown_model_log,
    resolve_capabilities,
)
from custom_components.marstek.const import (
    DEVICE_VENUS_A,
    DEVICE_VENUS_C,
    DEVICE_VENUS_D,
    DEVICE_VENUS_E,
    DEVICE_VENUS_E_MINI,
    DOMAIN,
    MANUAL_SET_AUTO,
    MANUAL_SLOTS_DEFAULT,
    MANUAL_SLOTS_E_MINI,
    MODE_AUTO,
    MODE_MANUAL,
    MODE_PASSIVE,
)
from custom_components.marstek.marstek_api import MarstekAPI

# Every model in the capability table, with the facts chapter 3.5, chapter 4
# and the manual_cfg table of the Open API (Rev 3.1) state about it.
KNOWN_MODELS = [
    # (reported model, display name, supports PV, manual slots, manual_set)
    ("VenusA", DEVICE_VENUS_A, True, MANUAL_SLOTS_DEFAULT, False),
    ("VenusC", DEVICE_VENUS_C, False, MANUAL_SLOTS_DEFAULT, False),
    ("VenusD", DEVICE_VENUS_D, True, MANUAL_SLOTS_DEFAULT, False),
    ("VenusE", DEVICE_VENUS_E, False, MANUAL_SLOTS_DEFAULT, False),
    ("VNSEM-0", DEVICE_VENUS_E_MINI, False, MANUAL_SLOTS_E_MINI, True),
]

PV_MODELS = ["VenusA", "Venus A", "VenusD", "Venus D"]
NON_PV_MODELS = ["VenusC", "Venus C", "VenusE", "Venus E", "VNSEM-0"]


@pytest.fixture(autouse=True)
def _forget_logged_models():
    """The unknown-model warning is emitted once per process, not per test."""
    reset_unknown_model_log()
    yield
    reset_unknown_model_log()


@pytest.fixture
def coordinator(hass, marstek_entry, mock_marstek_api):
    marstek_entry.add_to_hass(hass)
    return MarstekDataUpdateCoordinator(hass, mock_marstek_api, marstek_entry)


# --------------------------------------------------------------------------
# Capability resolution
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reported", "display", "supports_pv", "slots", "manual_set"), KNOWN_MODELS
)
def test_every_known_model_resolves(reported, display, supports_pv, slots, manual_set):
    """Each documented model resolves to its own capability profile."""
    capabilities = resolve_capabilities(reported)
    assert capabilities.known
    assert capabilities.model == display
    assert capabilities.supports_pv is supports_pv
    assert capabilities.manual_slots == slots
    assert capabilities.supports_manual_set is manual_set


@pytest.mark.parametrize("reported", PV_MODELS)
def test_venus_a_and_d_support_pv(reported):
    """Chapter 3.5: "PV (only for Venus A/D)"."""
    assert resolve_capabilities(reported).supports_pv


@pytest.mark.parametrize("reported", NON_PV_MODELS)
def test_other_models_do_not_support_pv(reported):
    """Venus C/E and the E mini have no PV component in chapter 4."""
    assert not resolve_capabilities(reported).supports_pv


def test_venus_e_mini_manual_differences():
    """The E mini has slots 0-5 and is the only model taking manual_set."""
    mini = resolve_capabilities("VNSEM-0")
    assert mini.manual_slots == MANUAL_SLOTS_E_MINI
    assert mini.supports_manual_set
    assert mini.is_valid_manual_slot(0)
    assert mini.is_valid_manual_slot(5)
    assert not mini.is_valid_manual_slot(6)
    assert not mini.is_valid_manual_slot(9)


@pytest.mark.parametrize("reported", ["VenusA", "VenusC", "VenusD", "VenusE"])
def test_full_size_models_keep_ten_manual_slots(reported):
    """Venus A/C/D/E support time_num 0-9 and reject manual_set."""
    capabilities = resolve_capabilities(reported)
    assert capabilities.manual_slots == MANUAL_SLOTS_DEFAULT
    assert not capabilities.supports_manual_set
    assert capabilities.is_valid_manual_slot(9)
    assert not capabilities.is_valid_manual_slot(10)


@pytest.mark.parametrize("slot", [-1, 1.5, "0", None, True, False])
def test_non_integer_manual_slots_are_rejected(slot):
    """A slot must be a plain in-range integer, never a bool or a string."""
    assert not resolve_capabilities("VenusE").is_valid_manual_slot(slot)


@pytest.mark.parametrize("capabilities", KNOWN_CAPABILITIES, ids=lambda c: c.model)
def test_all_known_models_offer_every_mode(capabilities):
    """No model is documented as lacking Auto, AI, Manual or Passive."""
    assert capabilities.es_modes == ALL_ES_MODES
    for mode in ALL_ES_MODES:
        assert capabilities.supports_mode(mode)
    assert not capabilities.supports_mode("Ups")


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        # Spelling variants seen in firmware and in the documentation.
        ("VenusC", DEVICE_VENUS_C),
        ("Venus C", DEVICE_VENUS_C),
        ("venus-c", DEVICE_VENUS_C),
        ("  VENUS_C  ", DEVICE_VENUS_C),
        ("Venus A", DEVICE_VENUS_A),
        ("VenusA 3.0", DEVICE_VENUS_A),
        ("VenusE 3.0", DEVICE_VENUS_E),
        ("VenusE Pro", DEVICE_VENUS_E),
        # The `src` field carries the serial; it must not defeat matching.
        ("VenusC-123456789012", DEVICE_VENUS_C),
        ("VenusE-24215edb178f", DEVICE_VENUS_E),
        ("VenusD-009b08a5ac28", DEVICE_VENUS_D),
        # SKU-style codes. Real E mini units report VNSEM-0.
        ("VNSEM-0", DEVICE_VENUS_E_MINI),
        ("VNSEM-0-ccc837b3a39f", DEVICE_VENUS_E_MINI),
        ("VNSE3-0", DEVICE_VENUS_E),
        ("VNSA-0", DEVICE_VENUS_A),
        ("VNSD-0", DEVICE_VENUS_D),
        # Documentation spellings of the E mini.
        ("Venus E mini", DEVICE_VENUS_E_MINI),
        ("VenusEmini", DEVICE_VENUS_E_MINI),
        ("Venus E-Mini", DEVICE_VENUS_E_MINI),
    ],
)
def test_model_matching_tolerates_firmware_spellings(reported, expected):
    """Casing, separators, serials and suffixes must not change the match."""
    assert resolve_capabilities(reported).model == expected


def test_e_mini_is_not_claimed_by_venus_e():
    """The longer E mini keys must win over the "venuse"/"vnse" prefixes."""
    for reported in ("VenusEmini", "VNSEM-0", "Venus E mini"):
        assert resolve_capabilities(reported).manual_slots == MANUAL_SLOTS_E_MINI


@pytest.mark.parametrize(
    ("value", "key"),
    [
        ("VenusC-123456789012", "venusc"),
        ("Venus C", "venusc"),
        ("VNSEM-0", "vnsem0"),
        ("Venus-24215ee580e7", "venus"),
        ("", ""),
        (None, ""),
        (123, ""),
    ],
)
def test_normalize_model(value, key):
    assert normalize_model(value) == key


# --------------------------------------------------------------------------
# Unknown models
# --------------------------------------------------------------------------


@pytest.mark.parametrize("reported", ["Jupiter", "HMG-50", "Venus", "Venus Z"])
def test_unknown_model_falls_back_without_failing(reported):
    """An unrecognised model still yields a usable baseline profile."""
    capabilities = resolve_capabilities(reported)
    assert not capabilities.known
    assert capabilities.model == reported
    # The baseline keeps every documented feature, so an unlisted model is no
    # worse off than it was before capabilities existed.
    assert capabilities.supports_pv
    assert capabilities.es_modes == ALL_ES_MODES
    assert capabilities.manual_slots == MANUAL_SLOTS_DEFAULT
    assert not capabilities.supports_manual_set


def test_unknown_model_is_logged_once(caplog):
    """One warning per model names it so it can be added to the table."""
    caplog.set_level(logging.DEBUG, logger="custom_components.marstek")
    for _ in range(5):
        resolve_capabilities("Jupiter")
        resolve_capabilities("Jupiter-123456789012")
    resolve_capabilities("Neptune")

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 2
    assert "'Jupiter'" in warnings[0].message
    assert "'Neptune'" in warnings[1].message
    assert "Unrecognised Marstek model" in warnings[0].message


@pytest.mark.parametrize("reported", [None, "", "   ", 42, {}])
def test_missing_model_is_not_reported_as_unknown(reported, caplog):
    """Metadata that has not arrived yet is not a new model to report."""
    caplog.set_level(logging.DEBUG, logger="custom_components.marstek")
    capabilities = resolve_capabilities(reported)
    assert not capabilities.known
    assert capabilities.supports_pv
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_capabilities_are_immutable_and_shared():
    """A frozen profile cannot be mutated by one config entry for another."""
    capabilities = resolve_capabilities("VenusD")
    assert resolve_capabilities("Venus D") is capabilities
    with pytest.raises(FrozenInstanceError):
        capabilities.supports_pv = False


def test_default_capabilities_label():
    assert default_capabilities().model == "Unknown"
    assert isinstance(default_capabilities(), MarstekCapabilities)


# --------------------------------------------------------------------------
# PV polling is gated by the resolved model
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", PV_MODELS)
async def test_pv_models_are_polled_for_pv(coordinator, mock_marstek_api, reported):
    """Venus A/D keep querying PV.GetStatus every cycle."""
    mock_marstek_api.get_device_info.return_value = {
        "device": reported,
        "ble_mac": "AA:BB:CC:DD:EE:01",
    }
    mock_marstek_api.get_pv_status.return_value = {"pv1_power": 120}
    for cycle in range(3):
        data = await coordinator._async_update_data()
        assert data["pv"] == {"pv1_power": 120}
        assert mock_marstek_api.get_pv_status.call_count == cycle + 1
    assert coordinator.capabilities.supports_pv


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", NON_PV_MODELS)
async def test_non_pv_models_are_never_polled_for_pv(
    coordinator, mock_marstek_api, reported
):
    """Venus C/E and the E mini never spend a request on PV.GetStatus."""
    mock_marstek_api.get_device_info.return_value = {
        "device": reported,
        "ble_mac": "AA:BB:CC:DD:EE:01",
    }
    mock_marstek_api.get_pv_status.return_value = {"pv1_power": 120}
    for _ in range(3):
        data = await coordinator._async_update_data()
        assert "pv" not in data
    assert mock_marstek_api.get_pv_status.call_count == 0
    # Skipping PV is not the same as the endpoint having failed.
    assert "pv" not in coordinator._disabled_optional_sections


@pytest.mark.asyncio
async def test_unknown_model_still_probes_pv(coordinator, mock_marstek_api):
    """A new model keeps its solar entities instead of silently losing them."""
    mock_marstek_api.get_device_info.return_value = {
        "device": "Venus Z",
        "ble_mac": "AA:BB:CC:DD:EE:01",
    }
    mock_marstek_api.get_pv_status.return_value = {"pv1_power": 55}
    assert (await coordinator._async_update_data())["pv"] == {"pv1_power": 55}


# --------------------------------------------------------------------------
# Entity creation follows the capability profile
# --------------------------------------------------------------------------

PV_ENTITY_KEYS = [
    "pv_power",
    *(
        f"pv{channel}_{field}"
        for channel in range(1, 5)
        for field in ("power", "voltage", "current", "state")
    ),
]


async def _setup_model(hass, entry, api, reported):
    """Set up a real config entry for one reported model."""
    api.get_device_info.return_value = {
        "device": reported,
        "ble_mac": "AA:BB:CC:DD:EE:01",
        "ver": 123,
    }
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return hass.data[DOMAIN][entry.entry_id]


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize("reported", PV_MODELS)
async def test_pv_entities_exist_for_pv_models(
    hass, marstek_entry, mock_marstek_api, reported, pv_status
):
    mock_marstek_api.get_pv_status.return_value = pv_status
    await _setup_model(hass, marstek_entry, mock_marstek_api, reported)
    registry = er.async_get(hass)
    for key in PV_ENTITY_KEYS:
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{marstek_entry.unique_id}_{key}"
        )
        assert entity_id is not None, key
        assert hass.states.get(entity_id).state not in (None, "unknown", "unavailable")


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize("reported", NON_PV_MODELS)
async def test_pv_entities_are_not_created_for_non_pv_models(
    hass, marstek_entry, mock_marstek_api, reported
):
    """A model without PV gets no permanently unavailable solar sensors."""
    await _setup_model(hass, marstek_entry, mock_marstek_api, reported)
    registry = er.async_get(hass)
    for key in PV_ENTITY_KEYS:
        assert (
            registry.async_get_entity_id(
                "sensor", DOMAIN, f"{marstek_entry.unique_id}_{key}"
            )
            is None
        ), key


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize("reported", ["VenusC", "VenusD", "VenusE", "VNSEM-0"])
async def test_battery_and_mode_entities_exist_for_every_model(
    hass, marstek_entry, mock_marstek_api, reported
):
    """Capability gating must not remove entities unrelated to PV."""
    await _setup_model(hass, marstek_entry, mock_marstek_api, reported)
    registry = er.async_get(hass)
    for platform, key in (
        ("sensor", "battery_soc"),
        ("sensor", "es_ongrid_power"),
        ("sensor", "es_total_pv_energy"),
        ("sensor", "passive_power_state"),
        ("binary_sensor", "battery_charging"),
        ("select", "operating_mode"),
        ("number", "passive_power"),
    ):
        assert (
            registry.async_get_entity_id(
                platform, DOMAIN, f"{marstek_entry.unique_id}_{key}"
            )
            is not None
        ), key


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize("reported", ["VenusC", "VenusD", "VNSEM-0", "Venus Z"])
async def test_mode_select_offers_the_models_modes(
    hass, marstek_entry, mock_marstek_api, reported
):
    """Every model currently offers all four modes."""
    await _setup_model(hass, marstek_entry, mock_marstek_api, reported)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "select", DOMAIN, f"{marstek_entry.unique_id}_operating_mode"
    )
    options = hass.states.get(entity_id).attributes["options"]
    assert options == list(ALL_ES_MODES)


# --------------------------------------------------------------------------
# Manual commands follow the capability profile
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e_mini_manual_command_sends_manual_set(coordinator, mock_marstek_api):
    """The E mini needs manual_set to give the slot a direction."""
    mock_marstek_api.get_device_info.return_value = {
        "device": "VNSEM-0",
        "ble_mac": "AA:BB:CC:DD:EE:01",
    }
    await coordinator._async_update_data()
    assert await coordinator.async_set_operating_mode(MODE_MANUAL)
    mock_marstek_api.set_es_mode_manual.assert_called_once_with(
        0, "00:00", "23:59", 127, 100, 1, MANUAL_SET_AUTO
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", ["VenusA", "VenusC", "VenusD", "VenusE"])
async def test_other_models_omit_manual_set(coordinator, mock_marstek_api, reported):
    """Venus A/C/D/E must keep sending the unchanged six-field manual_cfg."""
    mock_marstek_api.get_device_info.return_value = {
        "device": reported,
        "ble_mac": "AA:BB:CC:DD:EE:01",
    }
    await coordinator._async_update_data()
    assert await coordinator.async_set_operating_mode(MODE_MANUAL)
    # The call is unchanged from before capabilities existed: no seventh argument.
    mock_marstek_api.set_es_mode_manual.assert_called_once_with(
        0, "00:00", "23:59", 127, 100, 1
    )


@pytest.mark.parametrize(
    ("manual_set", "expected"),
    [(None, {}), (MANUAL_SET_AUTO, {"manual_set": 3}), (0, {"manual_set": 0})],
)
def test_api_includes_manual_set_only_when_given(manual_set, expected):
    """manual_cfg keeps its documented six fields unless manual_set is passed."""
    api = MarstekAPI("192.0.2.1")
    with patch("custom_components.marstek.marstek_api.socket.socket") as socket:
        connection = socket.return_value.__enter__.return_value
        connection.recvfrom.return_value = (
            json.dumps({"id": 1, "result": {"set_result": True}}).encode(),
            ("192.0.2.1", 30000),
        )
        assert api.set_es_mode_manual(0, "00:00", "23:59", 127, 100, 1, manual_set)
        payload = json.loads(connection.sendto.call_args.args[0])
    assert payload["params"]["config"]["manual_cfg"] == {
        "time_num": 0,
        "start_time": "00:00",
        "end_time": "23:59",
        "week_set": 127,
        "power": 100,
        "enable": 1,
        **expected,
    }


@pytest.mark.asyncio
async def test_unsupported_mode_is_refused_without_a_request(
    coordinator, mock_marstek_api, caplog
):
    """A mode the model does not declare never reaches the device."""
    caplog.set_level(logging.DEBUG, logger="custom_components.marstek")
    await coordinator._async_update_data()
    coordinator._capabilities = MarstekCapabilities(
        model="Venus Q", es_modes=(MODE_AUTO,)
    )
    assert not await coordinator.async_set_operating_mode(MODE_MANUAL)
    assert not await coordinator.async_set_operating_mode(MODE_PASSIVE)
    mock_marstek_api.set_es_mode_manual.assert_not_called()
    assert "does not support this mode" in caplog.text


@pytest.mark.asyncio
async def test_supported_mode_still_reaches_the_device(coordinator, mock_marstek_api):
    """Gating must not block the modes a model does declare."""
    await coordinator._async_update_data()
    assert await coordinator.async_set_operating_mode(MODE_AUTO)
    mock_marstek_api.set_es_mode_auto.assert_called_once()
