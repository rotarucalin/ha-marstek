# Marstek Home Assistant Integration - Complete Package

## Overview

This is a complete, production-ready Home Assistant custom integration for Marstek battery systems (Venus A, Venus C, Venus D, Venus E and Venus E mini models). The integration provides full local control over your Marstek device using the UDP-based Open API.

## Package Contents

### Core Integration Files (`custom_components/marstek/`)

1. **manifest.json** - Integration metadata and requirements
2. **__init__.py** - Main integration setup and coordinator
3. **const.py** - Constants and configuration values
4. **config_flow.py** - Configuration flow for UI setup
5. **marstek_api.py** - Complete API implementation for Marstek devices
6. **sensor.py** - All sensor entities (22 sensors)
7. **binary_sensor.py** - Binary sensor entities (4 sensors)
8. **select.py** - Operating mode selection
9. **number.py** - Power control for Passive mode
10. **strings.json** - UI strings and translations

### Documentation Files

1. **README.md** - Comprehensive user documentation with features, setup, and examples
2. **INSTALLATION.md** - Detailed step-by-step installation guide
3. **example_configuration.yaml** - Advanced configuration examples with automations
4. **lovelace_examples.yaml** - Dashboard card examples
5. **LICENSE** - MIT License with disclaimer

## Features Implemented

### Monitoring Capabilities
- ✅ Battery state of charge, temperature, capacity
- ✅ Real-time power flow (battery, grid, solar, load)
- ✅ Cumulative energy statistics
- ✅ WiFi signal strength
- ✅ Bluetooth connectivity status
- ✅ 3-phase energy meter support (CT sensor)
- ✅ Charging/discharging status

### Control Features
- ✅ Operating mode selection (Auto/AI/Manual/Passive)
- ✅ Direct power control in Passive mode (-3000W to +3000W)
- ✅ Configuration via Home Assistant UI
- ✅ Device discovery via UDP broadcast

### Integration Quality
- ✅ Full type hints and documentation
- ✅ Error handling and logging
- ✅ Coordinator pattern for efficient updates
- ✅ Proper entity naming and device info
- ✅ Energy Dashboard compatible
- ✅ HACS compatible structure

## Supported Device Models

### Venus C/E
Components: Marstek, WiFi, Bluetooth, Battery, ES, EM

### Venus A/D
Components: Marstek, WiFi, Bluetooth, Battery, PV, ES, EM

### Venus E mini
Components: Marstek, WiFi, Bluetooth, Battery, ES, EM
Manual mode: time periods 0-5 only, and `manual_cfg` carries `manual_set`.

Capabilities live in one table in `capabilities.py`, keyed by the model the
device reports. An unlisted model still sets up on the full documented API and
logs its name once.

## Entity Summary

### Sensors (22 total)
**Battery (4):**
- Battery State of Charge (%)
- Battery Temperature (°C)
- Battery Capacity (Wh)
- Battery Rated Capacity (Wh)

**Solar - Venus A and Venus D only (3):**
- Solar Power (W)
- Solar Voltage (V)
- Solar Current (A)

**Energy System (8):**
- Battery Power (W)
- Grid Power (W)
- Off-Grid Power (W)
- Total Solar Energy (Wh)
- Total Grid Output Energy (Wh)
- Total Grid Input Energy (Wh)
- Total Load Energy (Wh)
- Operating Mode

**Energy Meter (4):**
- Total Meter Power (W)
- Phase A Power (W)
- Phase B Power (W)
- Phase C Power (W)

**Network (1):**
- WiFi Signal Strength (dBm)

### Binary Sensors (4 total)
- Battery Charging
- Battery Discharging
- Bluetooth Connected
- CT Connected

### Controls (2 total)
- Operating Mode Select (Auto/AI/Manual/Passive)
- Passive Mode Power Number (-3000 to 3000W)

## API Implementation

The `marstek_api.py` file provides a complete Python implementation of the Marstek Open API:

- UDP JSON-RPC communication
- Device discovery via broadcast
- All status query methods
- Mode configuration methods
- Error handling and timeouts
- Logging support

### Supported API Methods
- `Marstek.GetDevice` - Device discovery
- `Wifi.GetStatus` - WiFi information
- `BLE.GetStatus` - Bluetooth status
- `Bat.GetStatus` - Battery information
- `PV.GetStatus` - Solar panel data
- `ES.GetStatus` - Energy system status
- `ES.GetMode` - Current operating mode
- `ES.SetMode` - Change operating mode
- `EM.GetStatus` - Energy meter data

## Installation Methods

### Method 1: HACS (Recommended)
1. Add custom repository in HACS
2. Install "Marstek Battery System"
3. Restart Home Assistant
4. Add integration via UI

### Method 2: Manual
1. Copy `custom_components/marstek/` to your config directory
2. Restart Home Assistant
3. Add integration via UI

## Configuration Requirements

**Before Installation:**
1. Marstek device connected to network
2. Open API enabled in Marstek mobile app
3. UDP port configured (default: 30000)
4. Device IP address (static IP recommended)

**Configuration via UI:**
1. Settings → Devices & Services
2. Add Integration → Search "Marstek"
3. Enter IP address and port
4. Integration automatically discovers device

## Usage Examples

### Basic Automation
```yaml
automation:
  - alias: "Charge at night"
    trigger:
      platform: time
      at: "01:00:00"
    action:
      - service: select.select_option
        target:
          entity_id: select.marstek_operating_mode
        data:
          option: "Passive"
      - service: number.set_value
        target:
          entity_id: number.marstek_passive_power
        data:
          value: -2000  # Charge at 2kW
```

### Energy Dashboard
Add to Energy Dashboard:
- Grid consumption: `sensor.marstek_total_grid_input_energy`
- Return to grid: `sensor.marstek_total_grid_output_energy`
- Solar production: `sensor.marstek_total_solar_energy`

## Technical Architecture

### Data Flow
1. **Coordinator** polls device every 30 seconds
2. **API Client** sends UDP JSON-RPC requests
3. **Device** responds with status data
4. **Entities** update from coordinator data
5. **UI** displays real-time information

### Update Mechanism
- Polling interval: 30 seconds (configurable)
- Data cached in coordinator
- All entities share same coordinator
- Automatic retry on communication errors

### Error Handling
- Connection timeouts (5 seconds default)
- JSON parsing errors
- Network failures
- Invalid responses
- Comprehensive logging

## File Structure
```
marstek_integration/
├── custom_components/
│   └── marstek/
│       ├── __init__.py              # Integration setup
│       ├── binary_sensor.py         # Binary sensors
│       ├── config_flow.py           # UI configuration
│       ├── const.py                 # Constants
│       ├── manifest.json            # Integration metadata
│       ├── marstek_api.py           # API client
│       ├── number.py                # Power control
│       ├── select.py                # Mode selection
│       ├── sensor.py                # Sensors
│       └── strings.json             # UI strings
├── README.md                        # User documentation
├── INSTALLATION.md                  # Installation guide
├── example_configuration.yaml       # Config examples
├── lovelace_examples.yaml          # Dashboard examples
└── LICENSE                          # MIT License
```

## Development Notes

### Code Quality
- Full Python type hints
- Comprehensive docstrings
- PEP 8 compliant formatting
- Error handling throughout
- Debug logging support

### Testing Recommendations
1. Test device discovery
2. Verify all sensors update
3. Test mode changes
4. Test power control
5. Verify error handling
6. Check Energy Dashboard compatibility

### Future Enhancements (Optional)
- [ ] Service for manual mode scheduling
- [ ] More granular update intervals per entity type
- [ ] Device diagnostics entity
- [ ] Configuration options for scan interval
- [ ] Support for multiple devices
- [ ] Statistics long-term storage

## API Protocol Details

**Transport:** UDP (User Datagram Protocol)
**Format:** JSON-RPC 2.0
**Port:** Configurable (default: 30000)
**Discovery:** UDP broadcast
**Timeout:** 5 seconds
**Encoding:** UTF-8

**Request Structure:**
```json
{
  "id": 0,
  "method": "Component.Method",
  "params": {
    "id": 0
  }
}
```

**Response Structure:**
```json
{
  "id": 0,
  "src": "VenusC-123456789012",
  "result": {
    "id": 0,
    ...
  }
}
```

## Compatibility

**Home Assistant:** 2023.1.0 or higher (recommended)
**Python:** 3.11+ (via Home Assistant)
**Network:** IPv4 local network required
**Devices:** Venus A, Venus C, Venus D, Venus E, Venus E mini

## Support and Contributing

**Issues:** Report bugs or request features via GitHub Issues
**Contributions:** Pull requests welcome
**Documentation:** Improvements and translations appreciated
**Community:** Share your automations and dashboards

## Disclaimer

This integration is provided "as is" for local use only. Marstek is not liable for any damages, data loss, or legal issues caused by your use of this integration. You are responsible for lawful and appropriate use.

The Local API access must be enabled by the user in the official Marstek app. Enabling the Open API may disable certain built-in device features to prevent command conflicts.

## License

MIT License - See LICENSE file for full text

---

## Quick Start Summary

1. **Enable Open API** in Marstek mobile app
2. **Install integration** via HACS or manually
3. **Restart** Home Assistant
4. **Add integration** via UI with IP and port
5. **Configure dashboard** with provided examples
6. **Create automations** for your use case

For detailed instructions, see INSTALLATION.md and README.md.

---

**Package Version:** 1.0.0  
**API Version:** Rev 1.0  
**Last Updated:** December 2025
