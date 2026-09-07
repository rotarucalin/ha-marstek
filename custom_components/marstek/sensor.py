"""Support for Marstek Battery System sensors."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MarstekDataUpdateCoordinator
from .const import DOMAIN, PASSIVE_POWER_STATES

_LOGGER = logging.getLogger(__name__)


@dataclass
class MarstekSensorEntityDescription(SensorEntityDescription):
    """Describes Marstek sensor entity."""

    value_fn: Callable[[dict], StateType] | None = None
    data_key: str | None = None


SENSOR_TYPES: tuple[MarstekSensorEntityDescription, ...] = (
    # Battery sensors
    MarstekSensorEntityDescription(
        key="battery_soc",
        name="Battery State of Charge",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        data_key="battery",
        value_fn=lambda data: data.get("soc"),
    ),
    MarstekSensorEntityDescription(
        key="battery_temperature",
        name="Battery Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        data_key="battery",
        value_fn=lambda data: data.get("bat_temp"),
    ),
    MarstekSensorEntityDescription(
        key="battery_capacity",
        name="Battery Capacity",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        data_key="battery",
        value_fn=lambda data: data.get("bat_capacity"),
    ),
    MarstekSensorEntityDescription(
        key="battery_rated_capacity",
        name="Battery Rated Capacity",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        data_key="battery",
        value_fn=lambda data: data.get("rated_capacity"),
    ),
    # PV sensors
    MarstekSensorEntityDescription(
        key="pv_power",
        name="Solar Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        data_key="pv",
        value_fn=lambda data: data.get("pv_power"),
    ),
    MarstekSensorEntityDescription(
        key="pv_voltage",
        name="Solar Voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        data_key="pv",
        value_fn=lambda data: data.get("pv_voltage"),
    ),
    MarstekSensorEntityDescription(
        key="pv_current",
        name="Solar Current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        data_key="pv",
        value_fn=lambda data: data.get("pv_current"),
    ),
    # Energy System sensors
    MarstekSensorEntityDescription(
        key="es_battery_power",
        name="Battery Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        data_key="es",
        value_fn=lambda data: data.get("bat_power"),
    ),
    MarstekSensorEntityDescription(
        key="es_ongrid_power",
        name="Grid Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        data_key="es",
        value_fn=lambda data: data.get("ongrid_power"),
    ),
    MarstekSensorEntityDescription(
        key="es_offgrid_power",
        name="Off-Grid Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        data_key="es",
        value_fn=lambda data: data.get("offgrid_power"),
    ),
    MarstekSensorEntityDescription(
        key="es_total_pv_energy",
        name="Total Solar Energy",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        data_key="es",
        value_fn=lambda data: data.get("total_pv_energy"),
    ),
    MarstekSensorEntityDescription(
        key="es_total_grid_output",
        name="Total Grid Output Energy",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        data_key="es",
        value_fn=lambda data: data.get("total_grid_output_energy"),
    ),
    MarstekSensorEntityDescription(
        key="es_total_grid_input",
        name="Total Grid Input Energy",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        data_key="es",
        value_fn=lambda data: data.get("total_grid_input_energy"),
    ),
    MarstekSensorEntityDescription(
        key="es_total_load_energy",
        name="Total Load Energy",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        data_key="es",
        value_fn=lambda data: data.get("total_load_energy"),
    ),
    MarstekSensorEntityDescription(
        key="es_mode",
        name="Operating Mode",
        data_key="es_mode",
        value_fn=lambda data: data.get("mode"),
    ),
    # Energy Meter sensors
    MarstekSensorEntityDescription(
        key="em_total_power",
        name="Total Meter Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        data_key="em",
        value_fn=lambda data: data.get("total_power"),
    ),
    MarstekSensorEntityDescription(
        key="em_phase_a_power",
        name="Phase A Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        data_key="em",
        value_fn=lambda data: data.get("a_power"),
    ),
    MarstekSensorEntityDescription(
        key="em_phase_b_power",
        name="Phase B Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        data_key="em",
        value_fn=lambda data: data.get("b_power"),
    ),
    MarstekSensorEntityDescription(
        key="em_phase_c_power",
        name="Phase C Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        data_key="em",
        value_fn=lambda data: data.get("c_power"),
    ),
    # WiFi sensor
    MarstekSensorEntityDescription(
        key="wifi_rssi",
        name="WiFi Signal Strength",
        native_unit_of_measurement="dBm",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        data_key="wifi",
        value_fn=lambda data: data.get("rssi"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Marstek sensors based on a config entry."""
    coordinator: MarstekDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    for description in SENSOR_TYPES:
        entities.append(MarstekSensor(coordinator, description))
    entities.append(MarstekPassivePowerStateSensor(coordinator))

    async_add_entities(entities)


class MarstekSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Marstek sensor."""

    entity_description: MarstekSensorEntityDescription

    def __init__(
        self,
        coordinator: MarstekDataUpdateCoordinator,
        description: MarstekSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description

        device_info = coordinator.data.get("device_info", {})
        device_name = device_info.get("device", "Marstek")
        ble_mac = device_info.get("ble_mac", "unknown")

        self._attr_unique_id = f"{ble_mac}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, ble_mac)},
            "name": f"{device_name} Battery System",
            "manufacturer": "Marstek",
            "model": device_info.get("device", "Unknown"),
            "sw_version": str(device_info.get("ver", "")),
        }

    @property
    def available(self) -> bool:
        """Return whether the entity is available."""
        key = self.entity_description.data_key
        return (
            super().available
            and key is not None
            and key in self.coordinator.data
            and self.coordinator.data[key] is not None
        )

    @property
    def native_value(self) -> StateType:
        """Return the state of the sensor."""
        if not self.available:
            return None

        data = self.coordinator.data[self.entity_description.data_key]

        if self.entity_description.value_fn:
            return self.entity_description.value_fn(data)

        return None


class MarstekPassivePowerStateSensor(CoordinatorEntity, SensorEntity):
    """Representation of the Marstek passive power control state."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = PASSIVE_POWER_STATES

    def __init__(self, coordinator: MarstekDataUpdateCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)

        device_info = coordinator.data.get("device_info", {})
        device_name = device_info.get("device", "Marstek")
        ble_mac = device_info.get("ble_mac", "unknown")

        self._attr_unique_id = f"{ble_mac}_passive_power_state"
        self._attr_name = "Passive Power State"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, ble_mac)},
            "name": f"{device_name} Battery System",
            "manufacturer": "Marstek",
            "model": device_info.get("device", "Unknown"),
            "sw_version": str(device_info.get("ver", "")),
        }

    @property
    def native_value(self) -> StateType:
        """Return the current passive power control state."""
        return self.coordinator.passive_power_state