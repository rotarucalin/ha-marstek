"""Learned mapping from desired real output to the power command sent.

The device treats a Passive power value as a raw command, not as a guaranteed
output, so the real output falls short of it by an amount that varies with the
operating point. This module records what command actually produced a given
desired output, per direction and per bucket of desired power, so the requested
value can mean the real output instead.

It is deliberately free of Home Assistant imports: everything here is pure data
and arithmetic driven by samples the coordinator supplies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .const import (
    CALIBRATION_STORAGE_VERSION,
    PASSIVE_BUCKET_WIDTH,
    PASSIVE_COMMAND_MAX,
    PASSIVE_COMMAND_MIN,
    PASSIVE_EMA_ALPHA,
    PASSIVE_MAX_STEP_W,
    SOURCE_CALIBRATION,
    SOURCE_DIRECT,
    SOURCE_EXTRAPOLATION,
    SOURCE_INTERPOLATION,
)

DIRECTION_CHARGE = "charge"
DIRECTION_DISCHARGE = "discharge"


def bucket_center(desired: float, width: int = PASSIVE_BUCKET_WIDTH) -> int:
    """Return the bucket a desired power belongs to, symmetric around zero.

    Charging and discharging are bucketed identically so that a -240 W request
    lands in the mirror image of the +240 W bucket.
    """
    magnitude = int(math.floor(abs(desired) / width + 0.5)) * width
    return -magnitude if desired < 0 else magnitude


def direction_of(desired: float) -> str:
    """Return which map a desired power belongs to."""
    return DIRECTION_CHARGE if desired < 0 else DIRECTION_DISCHARGE


@dataclass(frozen=True)
class ObserveResult:
    """The outcome of folding one measured sample into the learned map."""

    bucket: int
    learned: float
    changed: bool
    saturated: bool


class PassiveCalibration:
    """Per-direction maps of desired power bucket to learned command power."""

    def __init__(
        self,
        *,
        bucket_width: int = PASSIVE_BUCKET_WIDTH,
        alpha: float = PASSIVE_EMA_ALPHA,
        command_min: int = PASSIVE_COMMAND_MIN,
        command_max: int = PASSIVE_COMMAND_MAX,
        max_step: int = PASSIVE_MAX_STEP_W,
    ) -> None:
        """Initialize an empty calibration."""
        self.bucket_width = bucket_width
        self.alpha = alpha
        self.command_min = command_min
        self.command_max = command_max
        self.max_step = max_step
        self._maps: dict[str, dict[int, float]] = {
            DIRECTION_CHARGE: {},
            DIRECTION_DISCHARGE: {},
        }

    @classmethod
    def from_dict(cls, data: object, **kwargs) -> PassiveCalibration:
        """Build a calibration from a persisted payload."""
        calibration = cls(**kwargs)
        calibration.load(data)
        return calibration

    @property
    def is_empty(self) -> bool:
        """Return whether nothing has been learned yet."""
        return not any(self._maps.values())

    def clamp(self, value: float) -> float:
        """Constrain a command power to the range the device accepts."""
        return max(self.command_min, min(self.command_max, value))

    def set_command_range(self, command_min: int, command_max: int) -> None:
        """Tighten or loosen the accepted range, re-clamping learned entries.

        Called when the effective device limit changes after construction —
        typically because the model was only confirmed (from a cached guess)
        after a live poll, or the configured Max Passive Power option changed.
        Already-learned command values outside the new range are clamped in
        place rather than discarded: the desired-output mapping they represent
        is still valid, only the achievable command may have moved.
        """
        self.command_min = command_min
        self.command_max = command_max
        for table in self._maps.values():
            for bucket in list(table):
                table[bucket] = self.clamp(table[bucket])

    def is_saturated(self, command: float) -> bool:
        """Return whether a command already sits at a device limit."""
        return command <= self.command_min or command >= self.command_max

    def clear(self) -> None:
        """Forget everything learned so far."""
        for table in self._maps.values():
            table.clear()

    def command_for(self, desired: int) -> tuple[int, str]:
        """Return the command power to send for a desired real output."""
        desired = int(desired)
        if desired == 0:
            return 0, SOURCE_DIRECT

        table = self._maps[direction_of(desired)]
        if not table:
            return int(round(self.clamp(desired))), SOURCE_DIRECT

        bucket = bucket_center(desired, self.bucket_width)
        if bucket in table:
            return int(round(self.clamp(table[bucket]))), SOURCE_CALIBRATION

        known = sorted(table)
        lower = [candidate for candidate in known if candidate < bucket]
        upper = [candidate for candidate in known if candidate > bucket]

        if lower and upper:
            low, high = lower[-1], upper[0]
            ratio = (desired - low) / (high - low)
            value = table[low] + ratio * (table[high] - table[low])
            return int(round(self.clamp(value))), SOURCE_INTERPOLATION

        # Outside the known range, carry the nearest bucket's offset rather than
        # its absolute command, which would be meaningless at a different power.
        nearest = lower[-1] if lower else upper[0]
        offset = table[nearest] - nearest
        return int(round(self.clamp(desired + offset))), SOURCE_EXTRAPOLATION

    def observe(self, desired: int, command: int, actual: float) -> ObserveResult:
        """Fold one valid sample into the learned map for its bucket.

        The candidate is the command that would have produced the desired output
        had the error been a constant offset. An unseen bucket is seeded from it
        directly, since there is no prior worth averaging against; a known bucket
        moves only a fraction of the way, so noise cannot swing it.
        """
        bucket = bucket_center(desired, self.bucket_width)
        table = self._maps[direction_of(desired)]

        candidate = command + (desired - actual)
        clamped = self.clamp(candidate)
        saturated = clamped != candidate

        previous = table.get(bucket)
        if previous is None:
            learned = self._step_limited(clamped, command)
        else:
            learned = self._step_limited(
                previous + self.alpha * (clamped - previous), previous
            )
        learned = self.clamp(learned)

        changed = previous is None or abs(learned - previous) > 1e-9
        table[bucket] = learned
        return ObserveResult(
            bucket=bucket, learned=learned, changed=changed, saturated=saturated
        )

    def to_dict(self) -> dict:
        """Return a JSON-serializable snapshot for persistence."""
        return {
            "version": CALIBRATION_STORAGE_VERSION,
            "bucket_width": self.bucket_width,
            **{
                direction: {
                    str(bucket): round(learned, 3)
                    for bucket, learned in sorted(table.items())
                }
                for direction, table in self._maps.items()
            },
        }

    def load(self, data: object) -> None:
        """Replace the learned maps, discarding anything unusable.

        A missing, foreign or differently bucketed payload leaves an empty
        calibration rather than raising, so a bad store can never block setup.
        """
        self.clear()
        if not isinstance(data, dict):
            return
        if data.get("version") != CALIBRATION_STORAGE_VERSION:
            return
        if data.get("bucket_width") != self.bucket_width:
            return

        for direction, table in self._maps.items():
            stored = data.get(direction)
            if not isinstance(stored, dict):
                continue
            for key, value in stored.items():
                try:
                    bucket = int(key)
                    learned = float(value)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(learned) or bucket == 0:
                    continue
                if bucket != bucket_center(bucket, self.bucket_width):
                    continue
                if direction_of(bucket) != direction:
                    continue
                table[bucket] = self.clamp(learned)

    def _step_limited(self, value: float, anchor: float) -> float:
        """Constrain how far one update may move a command."""
        return max(anchor - self.max_step, min(anchor + self.max_step, value))
