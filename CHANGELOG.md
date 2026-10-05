# Changelog

## Unreleased

### Changed: Much lower read polling, with a temporary fast profile for control

The Venus firmware stays unreliable under Open API read load, so normal polling
is reduced by about 70% (from about 490 to about 144 requests per hour):

| Endpoint | Before | Normal | Active |
| --- | --- | --- | --- |
| `ES.GetStatus` | 30 s | 60 s | 30 s |
| `ES.GetMode` | 60 s (300 s in stable Passive) | 120 s | 30 s |
| `Bat.GetStatus` | 60 s | 180 s | 60 s |
| `PV.GetStatus` | 60 s | 300 s | — |
| `EM.GetStatus` | 60 s | 300 s | — |
| `Wifi.GetStatus` | 300 s | 900 s | — |
| `BLE.GetStatus` | 30 s | 600 s | — |

The active profile starts after any `ES.SetMode` (successful or failed), a
Passive verification mismatch, a suspected physical interruption, or Passive
retry activity. It ends once the mode is verified and, under Passive, the output
is acknowledged and stable, or at most 300 seconds after the last such event.
It never sends commands of its own. Endpoint backoff and degraded API mode take
precedence over both profiles.

Other effects:

- ES telemetry now counts as stale after 120 seconds instead of 60, matching
  the slower ES cadence.
- Dashboard values for battery, PV, meter and diagnostics update less often.
- Passive calibration learns more slowly, because it needs ES, mode and battery
  readings from the same cycle.
- The BLE failure-backoff cap rises from 300 to 600 seconds.
- Debug poll logs now include `effective_interval` and `profile=normal|active`.

Request pacing, request-ID validation, keepalive/retry logic, calibration
rules, services and entities are unchanged.

### Fixed: Total Solar Energy scaling (statistics-affecting)

`sensor.marstek_total_solar_energy` (unique ID suffix `es_total_pv_energy`)
reads `ES.GetStatus` `total_pv_energy`, which the Open API documents in units of
0.01 kWh. Previous versions published the raw count labelled as **Wh**, which
understated solar production by a factor of 10. The sensor now publishes
**kWh** (`raw / 100`, so raw 100 is 1.0 kWh), with device class `energy` and
state class `total_increasing`.

**Existing long-term statistics for this sensor contain historically incorrect
values.** Because the unit changes from Wh to kWh on the same entity, Home
Assistant will report a unit change for its statistics under
**Developer tools → Statistics**. You can either:

- update the statistics unit there and keep the old, incorrectly scaled
  history, or
- clear the old statistics for this entity and start fresh.

The Energy Dashboard's past solar figures from this sensor are wrong by the
same factor and are not corrected automatically. No other energy sensor
changes. Total Grid Output, Total Grid Input and Total Load Energy stay in Wh.

### Changed: Battery charging/discharging binary sensors renamed

`charg_flag` and `dischrg_flag` from `Bat.GetStatus` are permission flags, not
activity indicators. The binary sensors are renamed to reflect that:

- "Battery Charging" → **Charging Allowed**
- "Battery Discharging" → **Discharging Allowed**

They no longer use the `battery_charging` device class, so their states show
as On/Off instead of Charging/Not charging. Unique IDs are unchanged, so
existing entities, their entity IDs and their history are kept, and no
duplicate entities are created. Only the default friendly name changes. Fresh
installations derive new entity IDs from the new names. Automations that read
these sensors as "the battery is charging right now" should use the sign of
`sensor.marstek_battery_power` instead.
