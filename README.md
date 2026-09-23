# Marstek Battery System Integration for Home Assistant

A custom Home Assistant integration for Marstek battery systems (Venus A, Venus C, Venus D, Venus E, Venus E mini) using the local UDP API.

## Device identity and multiple batteries

Add each physical battery as a separate integration entry using its own IP
address. The integration identifies batteries by their BLE MAC address, so two
batteries of the same model remain separate and commands target the battery
owning the selected entity. A temporary failure to read device information at
startup reuses the saved identity and metadata. Entries without a valid identity
wait for Home Assistant to retry setup.

On the first successful setup after updating, the integration repairs device and
entity records created with an `unknown`, empty, or `None` identity. A lone
placeholder is corrected in place, preserving entity IDs and device IDs. When
both placeholder and real-MAC records exist, the real records are retained and
duplicate placeholder records are removed. Home Assistant logs the removed and
retained IDs; update any dashboard or automation that referenced a removed
duplicate. Migration only handles records owned by the relevant config entry.

## Features

- 📊 **Real-time Monitoring**: Battery SOC, power, temperature, and capacity
- ⚡ **Energy Tracking**: Solar generation, grid import/export, load consumption
- 🔋 **Battery Control**: Charging and discharging status
- &#x1F310; **Network Status**: WiFi signal strength and connectivity
- 📈 **Energy Meter**: CT sensor support for 3-phase power monitoring
- &#x1F39B;&#xFE0F; **Operating Modes**: Auto, AI, Manual, and Passive control modes
- 🔌 **Local Control**: Works entirely on your local network (no cloud required)

## Supported Devices

Every model in chapter 4 of the Marstek Device Open API is supported. They all
expose WiFi, Bluetooth, Battery, Energy System and Energy Meter components, and
all four operating modes. The differences the integration acts on are below.

| Model | Solar (PV) sensors | Manual time periods | Passive/Manual power limit | Notes |
| --- | --- | --- | --- | --- |
| Venus A | Yes | 0-9 | 1500 W | |
| Venus C | No | 0-9 | 3000 W (no documented rating) | |
| Venus D | Yes | 0-9 | 2200 W | |
| Venus E | No | 0-9 | 2500 W | |
| Venus E mini | No | 0-5 | 3000 W (no documented rating) | Manual commands also carry `manual_set` |

Power limits are conservative ceilings taken from chapter 4 of the Open API where it documents a rating; a specific unit may be rated lower. Venus C and the Venus E mini have no documented rating and fall back to the same 3000 W ceiling used for an unrecognised model. Your configured **Max Passive Power** option can only tighten this further, never raise it above the model's own limit.

The integration reads the model from the device and looks it up in a capability
table, tolerating firmware spelling differences such as `VenusC`, `Venus C` and
`VNSEM-0`. Solar sensors and PV polling exist only for models that answer the PV
API, so a Venus C or Venus E no longer carries three permanently unavailable
solar entities.

A model that is not in the table still sets up. It falls back to the full
documented API and logs its reported name once, so please open an issue with
that log line and the model can be added.

The protocol itself is summarised in
[docs/MARSTEK_OPEN_API.md](docs/MARSTEK_OPEN_API.md), including which fields
each model exposes and where the official document contradicts itself.

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
6. Enter your device's actual Max Passive Power in watts (default: 3000; lower this if your model's real charge/discharge limit is smaller)
7. Click **Submit**

The integration will automatically discover your device and create all available entities. Max Passive Power can be changed later from the integration's **Configure** option without re-adding the device.

## Entities

The integration creates the following entities:

### Sensors

**Battery**
- `sensor.marstek_battery_state_of_charge` - Battery percentage (%)
- `sensor.marstek_battery_temperature` - Battery temperature (°C)
- `sensor.marstek_battery_capacity` - Current battery capacity (Wh)
- `sensor.marstek_battery_rated_capacity` - Maximum battery capacity (Wh)

**Solar (Venus A and Venus D only)**
- `sensor.marstek_solar_power` - Derived sum of available PV1-PV4 power readings (W)
- `sensor.marstek_pv1_power` through `sensor.marstek_pv4_power` - Per-input power (W)
- `sensor.marstek_pv1_voltage` through `sensor.marstek_pv4_voltage` - Per-input voltage (V)
- `sensor.marstek_pv1_current` through `sensor.marstek_pv4_current` - Per-input current (A)
- `sensor.marstek_pv1_state` through `sensor.marstek_pv4_state` - Per-input state (`standby` or `working`)

Missing or null PV readings are unavailable. Solar Power sums only reported
channel powers and is unavailable when none are reported; an actual zero remains
zero. The old generic Solar Voltage and Solar Current mappings are removed.

**Energy System**
- `sensor.marstek_battery_power` - Battery charge/discharge power (W)
- `sensor.marstek_grid_power` - Grid import/export power (W)
- `sensor.marstek_off_grid_power` - Off-grid power usage (W)
- `sensor.marstek_total_solar_energy` - Cumulative solar generation (Wh)
- `sensor.marstek_total_grid_output_energy` - Cumulative grid export (Wh)
- `sensor.marstek_total_grid_input_energy` - Cumulative grid import (Wh)
- `sensor.marstek_total_load_energy` - Cumulative load consumption (Wh)
- `sensor.marstek_operating_mode` - Current operating mode
- `sensor.marstek_passive_power_state` - Passive power control state: `unknown`, `sent`, `acknowledged`, or `retrying`

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
- `number.marstek_passive_power` - Set power in Passive mode (±Max Passive Power, default ±3000 W; see [Configuration](#configuration))

## Operating Modes

### Auto Mode
The device operates automatically based on built-in algorithms.

### AI Mode
The device uses AI-based optimization for charging and discharging.

### Manual Mode
Create custom schedules for charging/discharging, one schedule slot (`time_num`) at a time. There is no default schedule: the API has no way to change mode to Manual without also naming a slot, so selecting "Manual" from the operating mode select entity always fails and points you at the `marstek.set_operating_mode_manual` service instead of guessing a schedule or silently rewriting an existing one. Use that service to write a slot.

### Passive Mode
Direct control of battery power. Use the `number.marstek_passive_power` entity or the `marstek.set_operating_mode_passive` service to control the battery:
- Positive values: Discharge to grid
- Negative values: Charge from grid
- The integration resends the configured power every 180 seconds to work around the device's passive-mode timeout. A failed send retains the target and retries after 15 seconds, repeating until a command succeeds and restores the normal 180-second cadence. This also covers failed initial commands and poll-driven verification resends.
- `sensor.marstek_passive_power_state` reports whether the target is `sent` (awaiting confirmation), `acknowledged` (confirmed within tolerance), `retrying` (confirmation failed, resent), or `unknown` (no target maintained).

**Note**: Selecting "Passive" via the operating mode select entity does **not** automatically send a power command. You must explicitly set the desired power using the number entity or the `set_operating_mode_passive` service. This prevents unintended intermediate power values when switching modes.

Only one Passive keepalive/retry timer is maintained per battery. New commands replace the pending timer and invalidate queued callbacks; retries use the current target. Selecting another operating mode, stopping Passive control, or unloading the integration cancels maintenance. No automation retry loop is needed. The Passive Power number reports the maintained target, while the separate Grid Power sensor reports the device's measured `ongrid_power`.

#### Adaptive power compensation

The device treats a Passive power value as a raw command, not a guaranteed output — inverter and standby losses mean asking for 240 W of discharge typically yields somewhat less. The `power` you set is the **desired real output**, not the raw command. The integration automatically learns, per direction (charge/discharge) and per 20 W bucket of desired power, what command actually produces that output, and applies it for you:

```
power: 240 (desired discharge) -> integration learns to send 275 W -> device reports ~240 W actual
```

- Learning only happens from valid, settled samples: Passive mode active and acknowledged, the command unchanged for at least 15 seconds, several consecutive stable readings, not mid comms-retry, and not blocked by SOC or a charge/discharge limit.
- Corrections are gradual (an exponential moving average, not a jump to the latest error) so the command converges without oscillating. Small errors (±10 W) are left alone.
- The learned mapping persists across Home Assistant restarts and interpolates between known buckets.
- Commands are clamped to this device's **effective** limit: the lower of your configured **Max Passive Power** option (default 3000 W, adjustable under **Settings → Devices & Services → Marstek Battery System → Configure**, or at initial setup) and a conservative ceiling for the model itself, taken from chapter 4 of the Open API where it documents one (Venus A 1500 W, Venus D 2200 W, Venus E 2500 W). Venus C, the Venus E mini and any unrecognised model have no documented rating and use the configured option alone, up to 3000 W. Raising the option above a known model's own ceiling has no effect; the model's limit still wins. If the desired output cannot be reached because the command is already pinned at that limit, the integration stops trying to increase it further and logs a WARNING once per bucket.
- The keepalive and retry timers always resend the compensated command, never the raw desired value, so a Home Assistant restart or a dropped Passive session doesn't regress to an uncompensated command.

Enable `custom_components.marstek: debug` in Home Assistant's logger configuration to trace operating-mode commands. New targets, retries, compensation changes, and mode selections include the device name, BLE MAC, host/port, mode, power (when applicable), and source:

- `new_target`: a target supplied through the Passive service or number entity.
- `keepalive`: the normal periodic refresh, logged only when it fails.
- `keepalive_retry`: a timer retry following a failed Passive send.
- `verification_retry`: a resend after fresh polling data fails to confirm the target.
- `compensation`: a resend after the learned command was adjusted to close the gap between desired and actual output.
- `operating_mode_select`: an explicit Auto or AI selection. Selecting Manual here always fails (see [Manual Mode](#manual-mode)); nothing is logged as a command since none is sent.
- `set_operating_mode_manual`: a Manual schedule slot written through the service.

Routine successful keepalives and timer scheduling are silent. A failed command produces one WARNING with the next action; underlying transport/protocol errors add DEBUG details. Retry attempts and their results remain visible at DEBUG. A command is logged as successful only when the API returns a truthy `set_result`. Confirmation of reported mode/power remains a separate polling step; a verification mismatch logs the desired and commanded power alongside the reported mode and power before retrying.

While a Passive target is maintained, a separate DEBUG line traces the three power values and where the command came from:

```
Marstek passive power: device=Marstek Venus A device_id=AA:BB:CC:DD:EE:01 desired=240W command=275W actual=241W source=compensation
```

`source` here is one of `direct` (no calibration yet, or zero power), `calibration` (an exact learned bucket), `interpolation` (between two learned buckets), `extrapolation` (beyond the learned range, carrying the nearest bucket's offset), `compensation` (a resend just triggered by a learning step), or `saturated` (the desired output can't be reached because the command is pinned at a device limit).

**Possible future improvement**: charge and discharge limits are tracked separately internally, but every model published so far has one rating for both, and the Max Passive Power option is still a single symmetric value. A device with genuinely different charge/discharge ratings would need separate `Max Charge Power` / `Max Discharge Power` options exposed in the UI to make use of that.

## Services

### marstek.set_operating_mode_manual

Write one Manual mode schedule slot (advanced users). Every field is sent exactly as given — nothing is inferred or read back from an existing slot, so an omitted field is never silently reused from what is already configured on the device.

```yaml
service: marstek.set_operating_mode_manual
data:
  entity_id: select.marstek_operating_mode
  time_num: 0  # Time period (0-9; the Venus E mini has only 0-5)
  start_time: "08:00"
  end_time: "20:00"
  week_set: 127  # Bitmask: 1=Mon, 3=Mon+Tue, 127=All week
  power: 500  # Power in watts; validated against this model's own limit
  enable: true
  # manual_set: 3  # Required on the Venus E mini, rejected on every other model
```

The service validates against the resolved device before sending anything: `time_num` must address a slot the model actually has, `power` must be within its effective limit (see [Adaptive power compensation](#adaptive-power-compensation) above), and `manual_set` (0=disable, 1=charge, 2=discharge, 3=auto) is required on the Venus E mini and rejected on every other model. A validation failure or a command the device refused raises an error to the caller rather than failing silently, so an automation sees it.

**Week Set Values:**
- Monday: 1
- Monday + Tuesday: 3
- All week: 127
- Calculate: Add powers of 2 (Mon=1, Tue=2, Wed=4, Thu=8, Fri=16, Sat=32, Sun=64)

### marstek.set_operating_mode_passive

Set and maintain passive mode power in a single call. Prefer this over the two-step approach (select mode + set number) in automations to avoid race conditions.

```yaml
service: marstek.set_operating_mode_passive
data:
  entity_id: select.marstek_operating_mode
  power: 800      # Desired real output in watts (-3000 to 3000). Positive = discharge, negative = charge.
  cd_time: 3600   # Retained for compatibility; ignored by the integration.
```

`power` is the real output you want, not the raw device command — see [Adaptive power compensation](#adaptive-power-compensation) above. Resolution failures or a device-rejected command raise an error to the caller rather than failing silently.

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
4. Add **Solar production**: `sensor.marstek_total_solar_energy` (Venus A and Venus D)
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

Closely spaced or overlapping requests may contribute to firmware/API timeouts
even when Wi-Fi reception is good. As a workaround, the integration serializes
polling and control requests through one gate per device client and leaves a
2.5-second quiet gap after each request completes,
including timeouts and API errors. This also prevents keepalive commands from
overlapping status queries. The 2.5-second interval is informed by
[another integration's hardware experience](https://github.com/arvdrpoo/ha-marstek-venus/blob/main/CHANGELOG.md#050---2026-07-15);
it still needs verification on this device and firmware. Increasing the socket
timeout alone does not solve requests that the firmware has already dropped.

PV and Bluetooth are optional. If `PV.GetStatus` or `BLE.GetStatus` fails, the
integration flags that section and stops querying it for the remainder of the
session. Its sensors become unavailable; stale readings are not kept for a
disabled section. Reload the Marstek integration or restart Home Assistant to
probe these endpoints again after connecting PV or enabling Bluetooth. A
transient failure also sets this flag, so reload if an installed component was
temporarily unresponsive. A successful response, including zero PV power or an
empty dictionary result, keeps the section in normal polling. Wi-Fi, battery,
energy-system, and operating-mode queries continue retrying normally.

Wi-Fi status is diagnostic data and is polled on the first update, then on the
first update at least five minutes after its last successful response. Its
signal-strength sensor retains that reading between requests. Failed Wi-Fi
requests retry each normal polling cycle, retaining cached data for up to six
misses. Reloading the integration or restarting Home Assistant resets the
Wi-Fi interval and queries it immediately. Battery, energy-system, and
operating-mode data continue to be queried every cycle.

Energy-meter polling (`EM.GetStatus`) also stops for the session after its first
response explicitly reporting `ct_state: 0` (CT disconnected). CT Connected,
Total Meter Power, and Phase A/B/C Power become unavailable immediately, and
cached meter readings are discarded. Reload the integration or restart Home
Assistant to probe the meter again after reconnecting the CT. Meter timeouts,
API errors, and responses without `ct_state` continue normal retrying; only an
explicit disconnected state disables this endpoint.

Passive command failures retain their existing 15-second retry and successful
commands their 180-second keepalive; the transport adds no immediate retries.
These timers run after the command completes; a command can also wait for an
in-flight request and the quiet interval. Poll cycles take longer with pacing.
The gap reduces request pressure but cannot guarantee that all device-side
failures disappear. The gate is per client and cannot coordinate traffic from
other integrations, apps, or API clients.

After replacing the integration's Python files, restart Home Assistant to load
the updated code. For subsequent optional-endpoint reprobes, an integration
reload is sufficient. With debug logging enabled for `custom_components.marstek`,
check fresh logs for:

- At most one failed `BLE.GetStatus` and one failed `PV.GetStatus` request per
  coordinator lifetime, each followed by the section's skip message.
- An energy-meter skip message after the CT reports disconnected, followed by
  no further `EM.GetStatus` queries until reload or restart.
- Continued essential polling and recovery after temporary timeouts.
- The frequency of `-32700` parse errors and essential endpoint timeouts,
  compared with the previous logs. Persistent failures need further device
  investigation; this workaround does not establish their cause.

Routine transport start/completion messages, successful keepalives, and timer
scheduling are omitted from DEBUG output. Request failures, other command
results, retries, calibration changes, and polling summaries are still logged.
A section's miss count and cache use share one message. Request serialization
and the 2.5-second gap apply regardless of logging verbosity.

1. Check firewall settings (UDP port must be open)
2. Ensure static IP is set for the device
3. Restart the Marstek device
4. Check network connectivity

### Entities Not Updating

1. Check the integration logs for errors
2. Verify the device is online in the Marstek app
3. Reload the integration from the UI
4. Check if Open API is still enabled

**Note on data caching**: Essential data sections retain their last known good values for six missed polling cycles, then become unavailable until a successful response. The time this spans depends on request latency and pacing as well as the 30-second polling interval. Failed optional PV/Bluetooth sections and an explicitly disconnected energy meter immediately become unavailable and remain disabled until integration reload or Home Assistant restart.

### Enable Debug Logging

Passive control acknowledges output only when fresh `ES.GetStatus` and
`ES.GetMode` power readings agree. The energy-system status remains the measured
output used by both verification and calibration. Cached, missing, invalid, or
conflicting readings cannot acknowledge a command or enter calibration.

An unexpected near-zero reading while charging, or disagreement between the
endpoints, triggers one follow-up round through the existing 2.5-second request
gate. A confirmed charging interruption resends the maintained compensated
command only with fresh charging permission and SOC below 100%. Unknown or denied
permission pauses automatic maintenance until fresh telemetry permits recovery;
the desired target is retained. Interruption samples are excluded from learning.

After a verification retry or compensation command, the poll waits for the
15-second settling period and reads battery, energy-system, and mode status
again before publishing. A failed follow-up leaves that section unavailable
instead of publishing a cached pre-command reading. The follow-up only verifies;
further recovery waits for the next poll or retry timer. New targets and mode
changes can still run while the poll waits for settling.

Debug records beginning `Marstek passive observation:` contain JSON with the
suspected interruption, confirmation, blocked recovery, and post-recovery phases.
They include both power readings and their freshness, mode, SOC, charging
permission, temperature, energy counters, and the preceding command's timestamp,
sequence, source, compensated power, and success. Successful keepalives appear in
this preceding-command context even though their routine messages are suppressed.
Energy counters retain the device's raw units; compare counters and timestamps
across events to investigate charging-progress relationships without assuming a
firmware cause or converting missing data to zero.

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
