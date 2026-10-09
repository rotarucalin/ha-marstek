# Changelog

## Unreleased

### Fixed: Passive control could lapse while the API was degraded

A keepalive schedules the next one only when it succeeds. After a failed
keepalive, the retry is the only thing that sends Passive again. While the API
was degraded, that retry was held back with no time limit. If the API stayed
degraded for the rest of the device's 3600-second `cd_time` countdown, the
device left Passive even though Home Assistant still held the target.

- The integration now records when the device last accepted a Passive write.
- A held-back keepalive retry is sent anyway once 10 minutes or less remain on
  the countdown. If it fails, it keeps retrying with the normal 30/60/90/120 s
  backoff.
- Outside that window, degraded mode still holds the retry back as before.

## 2.8.0.0

### Changed: Late UDP replies are correlated instead of discarded

A reply that arrived after its request timed out used to be lost. Each request
opened its own socket, so the reply reached a closed port, and request-ID
validation would have rejected it anyway because a newer request was active.

- Each device now uses one long-lived UDP socket, so late replies are still
  delivered. Discovery keeps its own short-lived broadcast socket.
- A bounded registry tracks the last 64 requests for 120 seconds, with request
  ID, method, send time, endpoint, read/write type, and completion or timeout
  state.
- A reply to a recently timed-out request is accepted as a late reply. Replies
  with unknown, expired or already answered IDs are ignored. Replies with
  `id=0` are still rejected as malformed.
- A late read updates only its own endpoint's cached data, and only if no
  newer request for that endpoint has produced data. For example, if request
  74 times out and request 75 succeeds, a late reply to 74 is discarded. If 75
  also failed, 74 is used.
- Late data is never treated as a fresh read. It does not acknowledge Passive
  power, satisfy mode verification, reset the miss count or affect backoff. It
  only updates what the integration serves until the next successful read.
- Data the integration drops on purpose, such as readings superseded by a
  Passive recovery command, cannot come back through a late reply. A mode
  write also supersedes ES status, ES mode and battery readings requested
  before it.
- Late `ES.SetMode` replies are logged but never applied. A command that
  already returned a failure stays failed. Acknowledgement still comes only
  from mode/status verification.
- New debug messages: `Accepted late response`, `Ignored late response because
  newer data exists`, `Ignored response with unknown or expired request ID`,
  `Ignored malformed response with id=0`, and `Correlated late response
  without applying it`. Each includes the request ID, the method and the age
  since TX, plus the newer request ID where relevant.

No requests or retries are added. Request serialization, the 2.5 s quiet gap,
timeouts, endpoint backoff, degraded API mode, polling intervals and SetMode
acknowledgement/retry logic are unchanged.

## 2.7.0.0

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
