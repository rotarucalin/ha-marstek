# Marstek Battery System Integration for Home Assistant

A custom Home Assistant integration for Marstek battery systems (Venus C, Venus E, Venus D) using the local UDP API.

## Features

- 📊 **Real-time Monitoring**: Battery SOC, power, temperature, and capacity
- ⚡ **Energy Tracking**: Solar generation, grid import/export, load consumption
- 🔋 **Battery Control**: Charging and discharging status
- 🌐 **Network Status**: WiFi signal strength and connectivity
- 📈 **Energy Meter**: CT sensor support for 3-phase power monitoring
- 🎛️ **Operating Modes**: Auto, AI, Manual, and Passive control modes
- 🔌 **Local Control**: Works entirely on your local network (no cloud required)

## Supported Devices

- **Venus C/E**: Battery systems with WiFi, Bluetooth, Battery, ES, and EM components
- **Venus D**: Battery systems with additional PV (photovoltaic) support

## Requirements

1. Marstek battery system connected to your local network (WiFi or Ethernet)
2. Open API feature enabled in the Marstek mobile app
3. Device IP address (can be found in the app or router settings)

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click on "Integrations"
3. Click the three dots in the top right corner
4. Select "Custom repositories"
5. Add this repository URL: `https://github.com/leonscheltema/ha-marstek`
6. Select category: "Integration"
7. Click "Add"
8. Search for "Marstek Battery System" in HACS
9. Click "Download"
10. Restart Home Assistant

### Manual Installation

1. Download the `marstek` folder from this repository
2. Copy the `marstek` folder to your `custom_components` directory in your Home Assistant configuration
3. Restart Home Assistant

## Configuration

### Enable Open API on Your Device

Before adding the integration, you must enable the Open API feature:

1. Open the Marstek mobile app
2. Navigate to your device settings
3. Enable the "Open API" feature
4. Set the UDP port (default: 30000, recommended: 49152-65535)
5. Note your device's IP address

**Important**: It's recommended to set a static IP address for your Marstek device in your router settings to prevent connection issues.

### Add Integration via UI

1. Go to **Settings** → **Devices & Services**
2. Click **+ Add Integration**
3. Search for **Marstek Battery System**
4. Enter your device's IP address
5. Enter the UDP port (default: 30000)
6. Click **Submit**

The integration will automatically discover your device and create all available entities.

## Entities

The integration creates the following entities:

### Sensors

**Battery**
- `sensor.marstek_battery_state_of_charge` - Battery percentage (%)
- `sensor.marstek_battery_temperature` - Battery temperature (°C)
- `sensor.marstek_battery_capacity` - Current battery capacity (Wh)
- `sensor.marstek_battery_rated_capacity` - Maximum battery capacity (Wh)

**Solar (Venus D only)**
- `sensor.marstek_solar_power` - Current solar generation (W)
- `sensor.marstek_solar_voltage` - Solar panel voltage (V)
- `sensor.marstek_solar_current` - Solar panel current (A)

**Energy System**
- `sensor.marstek_battery_power` - Battery charge/discharge power (W)
- `sensor.marstek_grid_power` - Grid import/export power (W)
- `sensor.marstek_off_grid_power` - Off-grid power usage (W)
- `sensor.marstek_total_solar_energy` - Cumulative solar generation (Wh)
- `sensor.marstek_total_grid_output_energy` - Cumulative grid export (Wh)
- `sensor.marstek_total_grid_input_energy` - Cumulative grid import (Wh)
- `sensor.marstek_total_load_energy` - Cumulative load consumption (Wh)
- `sensor.marstek_operating_mode` - Current operating mode

**Energy Meter (if CT connected)**
- `sensor.marstek_total_meter_power` - Total power from CT (W)
- `sensor.marstek_phase_a_power` - Phase A power (W)
- `sensor.marstek_phase_b_power` - Phase B power (W)
- `sensor.marstek_phase_c_power` - Phase C power (W)

**Network**
- `sensor.marstek_wifi_signal_strength` - WiFi signal strength (dBm)

### Binary Sensors

- `binary_sensor.marstek_battery_charging` - Battery charging status
- `binary_sensor.marstek_battery_discharging` - Battery discharging status
- `binary_sensor.marstek_bluetooth_connected` - Bluetooth connection status
- `binary_sensor.marstek_ct_connected` - CT sensor connection status

### Controls

- `select.marstek_operating_mode` - Select operating mode (Auto/AI/Manual/Passive)
- `number.marstek_passive_power` - Set power in Passive mode (-3000 to 3000 W)

## Operating Modes

### Auto Mode
The device operates automatically based on built-in algorithms.

### AI Mode
The device uses AI-based optimization for charging and discharging.

### Manual Mode
Create custom schedules for charging/discharging. When switching to Manual mode via the select entity, it defaults to:
- Time period: 0
- Schedule: 00:00-23:59
- Days: All week (Monday-Sunday)
- Power: 100W
- Enabled

For advanced manual scheduling, use the service calls described below.

### Passive Mode
Direct control of battery power. Use the `number.marstek_passive_power` entity or the `marstek.set_operating_mode_passive` service to control the battery:
- Positive values: Discharge to grid
- Negative values: Charge from grid
- Default countdown when using the number entity: 3600 seconds (1 hour)

**Note**: Selecting "Passive" via the operating mode select entity does **not** automatically send a power command. You must explicitly set the desired power using the number entity or the `set_operating_mode_passive` service. This prevents unintended intermediate power values when switching modes.

## Services

### marstek.set_operating_mode_manual

Set detailed manual mode schedule (advanced users).

```yaml
service: marstek.set_operating_mode_manual
data:
  entity_id: select.marstek_operating_mode
  time_num: 0  # Time period (0-9)
  start_time: "08:00"
  end_time: "20:00"
  week_set: 127  # Bitmask: 1=Mon, 3=Mon+Tue, 127=All week
  power: 500  # Power in watts
  enable: true
```

**Week Set Values:**
- Monday: 1
- Monday + Tuesday: 3
- All week: 127
- Calculate: Add powers of 2 (Mon=1, Tue=2, Wed=4, Thu=8, Fri=16, Sat=32, Sun=64)

### marstek.set_operating_mode_passive

Set passive mode with explicit power and countdown in a single call. Prefer this over the two-step approach (select mode + set number) in automations to avoid race conditions.

```yaml
service: marstek.set_operating_mode_passive
data:
  entity_id: select.marstek_operating_mode
  power: 800      # Power in watts (-3000 to 3000). Positive = discharge, negative = charge.
  cd_time: 3600   # Countdown in seconds (1 to 86400)
```

## Automation Examples

### Charge Battery During Cheap Electricity

```yaml
automation:
  - alias: "Charge Battery at Night"
    trigger:
      - platform: time
        at: "01:00:00"
    action:
      - service: marstek.set_operating_mode_passive
        data:
          entity_id: select.marstek_operating_mode
          power: -2000   # Charge at 2000W
          cd_time: 3600  # Run for 1 hour
```

### Discharge to Grid During Peak Hours

```yaml
automation:
  - alias: "Discharge During Peak"
    trigger:
      - platform: time
        at: "17:00:00"
    action:
      - service: marstek.set_operating_mode_passive
        data:
          entity_id: select.marstek_operating_mode
          power: 1500    # Discharge at 1500W
          cd_time: 3600  # Run for 1 hour
```

### Return to Auto Mode

```yaml
automation:
  - alias: "Return to Auto Mode"
    trigger:
      - platform: time
        at: "22:00:00"
    action:
      - service: select.select_option
        target:
          entity_id: select.marstek_operating_mode
        data:
          option: "Auto"
```

## Energy Dashboard Integration

You can add the Marstek sensors to Home Assistant's Energy Dashboard:

1. Go to **Settings** → **Dashboards** → **Energy**
2. Add **Grid consumption**: `sensor.marstek_total_grid_input_energy`
3. Add **Return to grid**: `sensor.marstek_total_grid_output_energy`
4. Add **Solar production**: `sensor.marstek_total_solar_energy` (Venus D)
5. Add **Battery systems**: 
   - Energy going in: Set up a template sensor based on positive `sensor.marstek_battery_power`
   - Energy going out: Set up a template sensor based on negative `sensor.marstek_battery_power`

## Troubleshooting

### Device Not Found

1. Verify the device IP address in your router or Marstek app
2. Ensure Open API is enabled in the Marstek app
3. Check that the device and Home Assistant are on the same network
4. Try pinging the device IP address from your Home Assistant host
5. Verify the UDP port matches (default: 30000)

### Connection Timeout

1. Check firewall settings (UDP port must be open)
2. Ensure static IP is set for the device
3. Restart the Marstek device
4. Check network connectivity

### Entities Not Updating

1. Check the integration logs for errors
2. Verify the device is online in the Marstek app
3. Reload the integration from the UI
4. Check if Open API is still enabled

**Note on data caching**: The integration caches the last known good values for each data section for up to 3 minutes (6 polling cycles at 30s intervals). If the device is briefly unreachable, entities will continue to show their last known values rather than becoming unavailable. After 3 minutes without a successful response, entities will report as unavailable.

### Enable Debug Logging

Add to `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.marstek: debug
```

## API Protocol

This integration uses the Marstek Open API via UDP with JSON-RPC format. The API provides:

- Device discovery via broadcast
- Query commands for status information
- Configuration commands for mode changes
- Real-time power and energy data

For detailed API documentation, refer to the Marstek Device Open API documentation.

## Support

For issues, feature requests, or questions:
- Open an issue on [GitHub](https://github.com/leonscheltema/ha-marstek/issues)
- Check existing issues for solutions
- Review the Marstek API documentation

## Contributing

Contributions are welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

## License

This project is licensed under the MIT License.

## Disclaimer

This integration is provided "as is" for local use only. Marstek is not liable for any damages, data loss, or legal issues caused by your use of this integration. You are responsible for lawful and appropriate use.

## Credits

Developed based on the Marstek Device Open API (Rev 1.0) documentation.
