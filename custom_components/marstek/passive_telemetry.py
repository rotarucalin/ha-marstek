"""Fresh, consistent telemetry used by passive control and diagnostics."""

from dataclasses import dataclass
from math import isfinite

from .const import PASSIVE_STABILITY_TOLERANCE_W

ZERO_POWER_TOLERANCE_W = 10


def numeric(value: object) -> float | None:
    """Accept finite API numbers, without treating booleans as Watts or SOC."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if isfinite(value) else None
    return None


def soc_value(value: object) -> float | None:
    """Read `Bat.GetStatus` `soc`, which the Open API types as a string."""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    return numeric(value)


@dataclass(frozen=True)
class PassiveTelemetry:
    """One observation; cached sections are never evidence for control."""

    mode: dict
    es: dict
    battery: dict
    fresh: frozenset[str]

    def power(self, section: str) -> float | None:
        if section not in self.fresh:
            return None
        data = self.es if section == "es" else self.mode
        return numeric(data.get("ongrid_power"))

    @property
    def disagreement(self) -> bool:
        status, mode = self.power("es"), self.power("es_mode")
        if status is None or mode is None:
            return False
        return abs(status - mode) > PASSIVE_STABILITY_TOLERANCE_W or (
            abs(status) <= ZERO_POWER_TOLERANCE_W
        ) != (abs(mode) <= ZERO_POWER_TOLERANCE_W)

    @property
    def actual(self) -> float | None:
        """Use the displayed ES output only when both fresh endpoints agree."""
        if self.power("es_mode") is None or self.disagreement:
            return None
        return self.power("es")

    def unexpected_zero(self, desired: int | None) -> bool:
        """A fresh zero in either endpoint merits confirmation, never learning."""
        return (
            desired is not None
            and abs(desired) > ZERO_POWER_TOLERANCE_W
            and any(
                power is not None and abs(power) <= ZERO_POWER_TOLERANCE_W
                for power in (self.power("es"), self.power("es_mode"))
            )
        )

    @property
    def charging_permitted(self) -> bool | None:
        """Return fresh permission, prohibition, or None when evidence is missing."""
        if "battery" not in self.fresh:
            return None
        flag = self.battery.get("charg_flag")
        soc = soc_value(self.battery.get("soc"))
        if soc is None and "es" in self.fresh:
            soc = numeric(self.es.get("bat_soc"))
        if flag in (False, 0):
            return False
        if flag not in (True, 1) or soc is None:
            return None
        return 0 <= soc < 100

    def diagnostic_data(self) -> dict:
        """Retain endpoint provenance and raw counter units for later comparison."""
        return {
            "fresh": sorted(self.fresh),
            "mode": self.mode.get("mode"),
            "es_power_w": numeric(self.es.get("ongrid_power")),
            "mode_power_w": numeric(self.mode.get("ongrid_power")),
            "battery_soc": soc_value(self.battery.get("soc")),
            "es_soc": numeric(self.es.get("bat_soc")),
            "mode_soc": numeric(self.mode.get("bat_soc")),
            "charg_flag": self.battery.get("charg_flag"),
            "dischrg_flag": self.battery.get("dischrg_flag"),
            "battery_temperature": numeric(self.battery.get("bat_temp")),
            "pv_power_w": numeric(self.es.get("pv_power")),
            "energy_counters_raw": {
                key: numeric(self.es.get(key))
                for key in (
                    "total_grid_input_energy",
                    "total_grid_output_energy",
                    "total_pv_energy",
                    "total_load_energy",
                )
            },
        }
