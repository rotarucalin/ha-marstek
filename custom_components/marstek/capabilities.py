"""Per-model capabilities of Marstek devices.

Chapter 4 of the Marstek Device Open API (Rev 3.1) lists which components each
model exposes, and the `manual_cfg` table gives the per-model Manual limits.
That knowledge lives here once, as a `MarstekCapabilities` value per model, so
the coordinator and the entity platforms never compare a model name directly.

Models the table does not know still set up. They fall back to the documented
baseline API, which is what every Marstek model published so far supports, and
the reported name is logged once so it can be added above.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .const import (
    DEFAULT_MAX_PASSIVE_POWER,
    DEVICE_VENUS_A,
    DEVICE_VENUS_C,
    DEVICE_VENUS_D,
    DEVICE_VENUS_E,
    DEVICE_VENUS_E_MINI,
    MANUAL_SET_AUTO,
    MANUAL_SET_DISABLE,
    MANUAL_SLOTS_DEFAULT,
    MANUAL_SLOTS_E_MINI,
    MODE_AI,
    MODE_AUTO,
    MODE_MANUAL,
    MODE_PASSIVE,
    PASSIVE_POWER_LIMIT_VENUS_A_W,
    PASSIVE_POWER_LIMIT_VENUS_D_W,
    PASSIVE_POWER_LIMIT_VENUS_E_W,
)

_LOGGER = logging.getLogger(__name__)

# Every ES mode this integration knows how to command. "Ups" is documented by
# the API but is deliberately not offered here.
ALL_ES_MODES: tuple[str, ...] = (MODE_AUTO, MODE_AI, MODE_MANUAL, MODE_PASSIVE)

UNKNOWN_MODEL = "Unknown"


@dataclass(frozen=True)
class MarstekCapabilities:
    """What one Marstek model exposes over the local Open API."""

    model: str
    """Canonical display name, or the reported string for an unknown model."""

    supports_pv: bool = False
    """Whether the model answers PV.GetStatus (API chapter 3.5: Venus A/D only)."""

    es_modes: tuple[str, ...] = ALL_ES_MODES
    """ES modes this model accepts through ES.SetMode."""

    manual_slots: int = MANUAL_SLOTS_DEFAULT
    """Number of Manual schedule slots addressable through `time_num`."""

    supports_manual_set: bool = False
    """Whether manual_cfg accepts `manual_set` (Venus E mini only)."""

    passive_charge_limit_w: int = DEFAULT_MAX_PASSIVE_POWER
    """Conservative ceiling (W) for a charge (negative) Passive/Manual command."""

    passive_discharge_limit_w: int = DEFAULT_MAX_PASSIVE_POWER
    """Conservative ceiling (W) for a discharge (positive) Passive/Manual command.

    Charge and discharge are tracked separately so a future model with an
    asymmetric rating needs only its own two values here, not a new field.
    They are equal for every model published so far because the Open API
    (Rev 3.1, chapter 4) documents a single power rating per model, not one
    per direction.
    """

    known: bool = True
    """False when the reported model was not in the table and defaults are used."""

    def supports_mode(self, mode: str) -> bool:
        """Return whether this model accepts an ES mode."""
        return mode in self.es_modes

    def is_valid_manual_slot(self, time_num: object) -> bool:
        """Return whether `time_num` addresses a Manual slot this model has."""
        if not isinstance(time_num, int) or isinstance(time_num, bool):
            return False
        return 0 <= time_num < self.manual_slots

    def is_valid_manual_set(self, manual_set: object) -> bool:
        """Return whether `manual_set` is one of the four documented values."""
        if not isinstance(manual_set, int) or isinstance(manual_set, bool):
            return False
        return MANUAL_SET_DISABLE <= manual_set <= MANUAL_SET_AUTO

    def passive_power_range(
        self, configured_limit: int | None = None
    ) -> tuple[int, int]:
        """Return the (min, max) command power in watts this model accepts.

        `configured_limit` is the user's own Max Passive Power option; when
        given, it can only tighten the range further, never loosen it beyond
        this model's own hardware ceiling. Without it, this returns the bare
        hardware limit, which is what a Manual command is checked against
        since it has no separate user-configurable ceiling.
        """
        charge_limit = self.passive_charge_limit_w
        discharge_limit = self.passive_discharge_limit_w
        if configured_limit is not None:
            charge_limit = min(charge_limit, configured_limit)
            discharge_limit = min(discharge_limit, configured_limit)
        return -charge_limit, discharge_limit


# Venus A/C/D/E support Manual slots 0-9 and reject `manual_set`; only the
# Venus E mini differs, and only Venus A/D answer PV.GetStatus. Venus C and
# the Venus E mini have no documented power rating (chapter 4), so they keep
# the conservative DEFAULT_MAX_PASSIVE_POWER fallback for both directions.
_VENUS_A = MarstekCapabilities(
    model=DEVICE_VENUS_A,
    supports_pv=True,
    passive_charge_limit_w=PASSIVE_POWER_LIMIT_VENUS_A_W,
    passive_discharge_limit_w=PASSIVE_POWER_LIMIT_VENUS_A_W,
)
_VENUS_C = MarstekCapabilities(model=DEVICE_VENUS_C)
_VENUS_D = MarstekCapabilities(
    model=DEVICE_VENUS_D,
    supports_pv=True,
    passive_charge_limit_w=PASSIVE_POWER_LIMIT_VENUS_D_W,
    passive_discharge_limit_w=PASSIVE_POWER_LIMIT_VENUS_D_W,
)
_VENUS_E = MarstekCapabilities(
    model=DEVICE_VENUS_E,
    passive_charge_limit_w=PASSIVE_POWER_LIMIT_VENUS_E_W,
    passive_discharge_limit_w=PASSIVE_POWER_LIMIT_VENUS_E_W,
)
_VENUS_E_MINI = MarstekCapabilities(
    model=DEVICE_VENUS_E_MINI,
    manual_slots=MANUAL_SLOTS_E_MINI,
    supports_manual_set=True,
)

KNOWN_CAPABILITIES: tuple[MarstekCapabilities, ...] = (
    _VENUS_A,
    _VENUS_C,
    _VENUS_D,
    _VENUS_E,
    _VENUS_E_MINI,
)

# Normalized model keys. A reported model matches when it *starts with* one of
# these, so firmware suffixes ("VenusE 3.0") still resolve. Longer keys are
# tried first, keeping "venusemini" and "vnsem" from being claimed by the
# shorter "venuse" and "vnse".
#
# Two families appear in the wild. Firmware reports either a marketing name
# ("VenusC", "Venus A", "VenusE 3.0") or an SKU code ("VNSEM-0"). The Venus E
# mini has only ever been observed reporting the SKU form, so "venusemini" is
# kept for documentation spellings while "vnsem" is what real units send.
_MODEL_KEYS: dict[str, MarstekCapabilities] = {
    "venusa": _VENUS_A,
    "venusc": _VENUS_C,
    "venusd": _VENUS_D,
    "venuse": _VENUS_E,
    "venusemini": _VENUS_E_MINI,
    "venusmini": _VENUS_E_MINI,
    "vnsa": _VENUS_A,
    "vnsc": _VENUS_C,
    "vnsd": _VENUS_D,
    "vnse": _VENUS_E,
    "vnsem": _VENUS_E_MINI,
}

_MATCH_ORDER: tuple[tuple[str, MarstekCapabilities], ...] = tuple(
    sorted(_MODEL_KEYS.items(), key=lambda item: len(item[0]), reverse=True)
)

# Reports sometimes carry the hardware serial, as in the `src` field's
# "VenusC-123456789012". Drop it before matching.
_SERIAL_SUFFIX = re.compile(r"[-_ ]+[0-9a-fA-F]{6,}$")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Models already reported through the unknown-model warning.
_logged_models: set[str] = set()


def normalize_model(value: object) -> str:
    """Reduce a reported model to a key, tolerating firmware spellings.

    "VenusC", "Venus C", "venus-c" and "VenusC-123456789012" all collapse to
    "venusc". Anything that is not a usable string collapses to "".
    """
    if not isinstance(value, str):
        return ""
    return _NON_ALNUM.sub("", _SERIAL_SUFFIX.sub("", value.strip()).lower())


def default_capabilities(model: str = UNKNOWN_MODEL) -> MarstekCapabilities:
    """Return the baseline profile used for a model that is not in the table.

    PV stays enabled so an unrecognised PV model keeps its solar entities. A
    model without PV simply fails the endpoint once and the coordinator stops
    asking, which is how every model behaved before capabilities existed.
    """
    return MarstekCapabilities(model=model, supports_pv=True, known=False)


def resolve_capabilities(reported_model: object) -> MarstekCapabilities:
    """Return the capabilities for the model a device reports.

    Never raises: an unusable or unrecognised model yields the baseline profile
    so setup continues.
    """
    key = normalize_model(reported_model)
    if key:
        for prefix, capabilities in _MATCH_ORDER:
            if key.startswith(prefix):
                return capabilities

    if not isinstance(reported_model, str) or not reported_model.strip():
        # Metadata is not available yet; a later poll resolves the real model.
        return default_capabilities()

    label = reported_model.strip()
    if key not in _logged_models:
        _logged_models.add(key)
        _LOGGER.warning(
            "Unrecognised Marstek model %r; using baseline capabilities "
            "(PV probed, modes %s, Manual slots 0-%s, no manual_set). Please "
            "report this model so it can be added to the capability table",
            label,
            "/".join(ALL_ES_MODES),
            MANUAL_SLOTS_DEFAULT - 1,
        )
    return default_capabilities(label)


def reset_unknown_model_log() -> None:
    """Forget which unknown models were logged. For tests only."""
    _logged_models.clear()
