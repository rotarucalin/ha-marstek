"""Detect unexpected fresh zero telemetry for either power direction."""

import pytest

from custom_components.marstek.passive_telemetry import PassiveTelemetry


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
