"""Unit tests for the learned desired-to-command power mapping.

This module only exercises ``custom_components.marstek.passive_calibration``,
which does not itself import Home Assistant. Home Assistant is still required
to be installed to run this file, because importing any submodule of the
``custom_components.marstek`` package first executes the package's
``__init__.py``.
"""

import pytest

from custom_components.marstek.const import (
    PASSIVE_COMMAND_MAX,
    PASSIVE_COMMAND_MIN,
    PASSIVE_EMA_ALPHA,
    PASSIVE_MAX_STEP_W,
    SOURCE_CALIBRATION,
    SOURCE_DIRECT,
    SOURCE_EXTRAPOLATION,
    SOURCE_INTERPOLATION,
)
from custom_components.marstek.passive_calibration import (
    PassiveCalibration,
    bucket_center,
    direction_of,
)


def test_bucket_center_rounds_to_nearest_width_symmetrically():
    assert bucket_center(245) == 240
    assert bucket_center(255) == 260
    assert bucket_center(-245) == -240
    assert bucket_center(10) == 20  # ties round away from zero


def test_direction_of_splits_on_sign():
    assert direction_of(240) == "discharge"
    assert direction_of(-240) == "charge"


def test_no_calibration_returns_desired_as_command():
    calibration = PassiveCalibration()
    assert calibration.is_empty
    assert calibration.command_for(240) == (240, SOURCE_DIRECT)
    assert calibration.command_for(-500) == (-500, SOURCE_DIRECT)


def test_zero_desired_is_always_direct_even_when_learned():
    calibration = PassiveCalibration()
    calibration.observe(240, 240, 205)
    assert calibration.command_for(0) == (0, SOURCE_DIRECT)


def test_learned_bucket_is_returned_as_calibration():
    calibration = PassiveCalibration()
    result = calibration.observe(240, 240, 205)
    assert result.bucket == 240
    assert result.learned == pytest.approx(275)
    assert result.changed is True
    assert result.saturated is False
    assert calibration.command_for(240) == (275, SOURCE_CALIBRATION)


def test_charge_and_discharge_maps_are_kept_separate():
    calibration = PassiveCalibration()
    calibration.observe(240, 240, 205)

    # Discharge at +240 was just learned; charge at -240 must be untouched.
    assert calibration.command_for(-240) == (-240, SOURCE_DIRECT)
    assert calibration.command_for(240) == (275, SOURCE_CALIBRATION)

    calibration.observe(-240, -240, -205)
    assert calibration.command_for(-240) == (-275, SOURCE_CALIBRATION)
    assert calibration.command_for(240) == (275, SOURCE_CALIBRATION)


def test_interpolation_between_two_known_buckets():
    calibration = PassiveCalibration()
    calibration.observe(220, 220, 200)  # candidate 240 -> learned 240
    calibration.observe(260, 260, 230)  # candidate 290 -> learned 290

    command, source = calibration.command_for(245)
    assert source == SOURCE_INTERPOLATION
    assert command == pytest.approx(271, abs=1)


def test_extrapolation_beyond_known_range_carries_nearest_offset():
    calibration = PassiveCalibration()
    calibration.observe(220, 220, 200)  # learned 240 -> offset +20
    calibration.observe(260, 260, 230)  # learned 290 -> offset +30

    command, source = calibration.command_for(400)
    assert source == SOURCE_EXTRAPOLATION
    assert command == 430  # nearest bucket (260) offset of +30 carried forward

    command, source = calibration.command_for(100)
    assert source == SOURCE_EXTRAPOLATION
    assert command == 120  # nearest bucket (220) offset of +20 carried forward


def test_gradual_update_moves_only_a_fraction_toward_the_candidate():
    calibration = PassiveCalibration()
    calibration.observe(240, 240, 205)  # seeds bucket at 275
    before = calibration.command_for(240)[0]

    result = calibration.observe(240, 275, 100)  # a much larger shortfall now
    candidate = 275 + (240 - 100)
    expected = before + PASSIVE_EMA_ALPHA * (candidate - before)
    assert result.learned == pytest.approx(expected)
    assert 0 < result.learned - before < candidate - before


def test_single_outlier_is_bounded_by_max_step():
    calibration = PassiveCalibration()
    calibration.observe(240, 240, 205)  # seeds bucket at 275
    before = calibration.command_for(240)[0]

    # An extreme, implausible sample must not swing the learned value freely.
    result = calibration.observe(240, before, -3000)
    assert result.learned - before == pytest.approx(PASSIVE_MAX_STEP_W)


def test_seeding_an_empty_bucket_is_also_step_limited():
    calibration = PassiveCalibration()
    # A wild first sample should not seed a bucket far from the command sent.
    result = calibration.observe(240, 240, -3000)
    assert result.learned - 240 == pytest.approx(PASSIVE_MAX_STEP_W)


def test_saturation_clamps_to_the_command_limit_and_reports_it():
    calibration = PassiveCalibration()
    result = calibration.observe(2980, 2980, 2000)
    assert result.saturated is True
    assert result.learned == PASSIVE_COMMAND_MAX
    assert calibration.is_saturated(result.learned)

    command, source = calibration.command_for(2980)
    assert command == PASSIVE_COMMAND_MAX
    assert source == SOURCE_CALIBRATION


def test_saturation_clamps_at_the_negative_limit_too():
    calibration = PassiveCalibration()
    result = calibration.observe(-2980, -2980, -2000)
    assert result.saturated is True
    assert result.learned == PASSIVE_COMMAND_MIN


def test_persistence_round_trip_preserves_both_maps():
    calibration = PassiveCalibration()
    calibration.observe(240, 240, 205)
    calibration.observe(-500, -500, -470)

    restored = PassiveCalibration.from_dict(calibration.to_dict())
    assert restored.command_for(240) == calibration.command_for(240)
    assert restored.command_for(-500) == calibration.command_for(-500)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "not a dict",
        {},
        {"version": 999, "bucket_width": 20, "discharge": {"240": 275}},
        {"version": 1, "bucket_width": 50, "discharge": {"240": 275}},
        {"version": 1, "bucket_width": 20, "discharge": {"241": 275}},
        {"version": 1, "bucket_width": 20, "discharge": {"-240": 275}},
        {"version": 1, "bucket_width": 20, "discharge": {"240": "nan"}},
        {"version": 1, "bucket_width": 20, "discharge": {"240": float("inf")}},
    ],
)
def test_corrupt_or_foreign_payloads_degrade_to_empty(payload):
    calibration = PassiveCalibration.from_dict(payload)
    assert calibration.is_empty
    assert calibration.command_for(240) == (240, SOURCE_DIRECT)


def test_clear_forgets_everything_learned():
    calibration = PassiveCalibration()
    calibration.observe(240, 240, 205)
    calibration.observe(-500, -500, -470)
    calibration.clear()
    assert calibration.is_empty
