# Changelog

## Unreleased

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
