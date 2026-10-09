"""Detect unexpected fresh zero telemetry for either power direction."""

import pytest

from custom_components.marstek.passive_telemetry import PassiveTelemetry, soc_value


@pytest.mark.parametrize(
    "desired,expected",
    [(-760, True), (340, True), (0, False), (None, False), (-10, False), (10, False)],
)
def test_unexpected_zero_requires_nonzero_target(desired, expected):
    telemetry = PassiveTelemetry(
        {"ongrid_power": 0}, {"ongrid_power": 0}, {}, frozenset({"es", "es_mode"})
    )
    assert telemetry.unexpected_zero(desired) is expected


@pytest.mark.parametrize("desired", [-760, 340])
@pytest.mark.parametrize("endpoint", ["es", "es_mode"])
@pytest.mark.parametrize(
    "reported,expected", [(0, True), (-10, True), (10, True), (11, False)]
)
def test_unexpected_zero_requires_either_endpoint_to_be_fresh(
    desired, endpoint, reported, expected
):
    telemetry = PassiveTelemetry(
        {"ongrid_power": reported if endpoint == "es_mode" else 0},
        {"ongrid_power": reported if endpoint == "es" else 0},
        {},
        frozenset({endpoint}),
    )
    assert telemetry.unexpected_zero(desired) is expected


@pytest.mark.parametrize("desired", [-760, 340])
def test_cached_zeros_are_not_suspect(desired):
    telemetry = PassiveTelemetry(
        {"ongrid_power": 0}, {"ongrid_power": 0}, {}, frozenset()
    )
    assert not telemetry.unexpected_zero(desired)


@pytest.mark.parametrize(
    "battery,es,fresh,expected",
    [
        ({"soc": 50, "charg_flag": True}, {}, {"battery"}, True),
        ({"soc": 50, "charg_flag": 1}, {}, {"battery"}, True),
        ({"soc": 50, "charg_flag": False}, {}, {"battery"}, False),
        ({"charg_flag": False}, {}, {"battery"}, False),
        ({"soc": 50, "charg_flag": 0}, {}, {"battery"}, False),
        ({"soc": 100, "charg_flag": True}, {}, {"battery"}, False),
        ({}, {}, {"battery"}, None),
        ({"soc": 50}, {}, {"battery"}, None),
        ({"soc": 50, "charg_flag": None}, {}, {"battery"}, None),
        ({"soc": 100, "charg_flag": None}, {}, {"battery"}, None),
        ({"charg_flag": True}, {}, {"battery"}, None),
        ({"soc": 50, "charg_flag": False}, {}, {"es"}, None),
        ({"soc": 50, "charg_flag": True}, {}, {"es"}, None),
        ({"charg_flag": True}, {"bat_soc": 50}, {"battery", "es"}, True),
        ({"charg_flag": True}, {"bat_soc": 50}, {"battery"}, None),
        ({"soc": "50", "charg_flag": True}, {}, {"battery"}, True),
        ({"soc": "100", "charg_flag": True}, {}, {"battery"}, False),
    ],
)
def test_charging_permission_requires_fresh_evidence(battery, es, fresh, expected):
    telemetry = PassiveTelemetry({}, es, battery, frozenset(fresh))
    assert telemetry.charging_permitted is expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (50, 50.0),
        (97.5, 97.5),
        ("50", 50.0),
        (" 97.5 ", 97.5),
        ("full", None),
        ("", None),
        ("nan", None),
        ("inf", None),
        (True, None),
        (None, None),
    ],
)
def test_soc_value_accepts_the_documented_string_type(value, expected):
    """Open API 3.4 types `soc` as a string; firmware sends a number."""
    assert soc_value(value) == expected
