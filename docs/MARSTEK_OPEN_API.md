# Marstek Device Open API (Rev 3.1) — working notes

Condensed from the official specification at
<https://static-eu.marstekenergy.com/ems/resource/agreement/MarstekDeviceOpenApi.pdf>
(22 pages, revision 3.1). This file exists so nobody has to re-download and
re-extract the PDF to answer a question about the protocol. Where the
integration deviates from the specification, that is recorded here too.

Section numbers below match the chapters in the PDF.

## 1. Transport and framing

Plain UDP on the local network. No TLS, no authentication, no session. The
owner must enable the Open API in the Marstek mobile app first; enabling it can
disable some built-in device features to avoid command conflicts.

Default port is 30000. The document recommends moving it into 49152-65535.
A static DHCP lease is recommended for long-term use.

Requests are JSON-RPC-ish objects:

```json
{"id": 1, "method": "ES.GetStatus", "params": {"id": 0}}
```

`params.id` is the instance ID and is `0` on every single-instance device. The
outer `id` is the caller's own request identifier and is echoed back.

Successful responses carry `result`; failures carry `error`:

```json
{"id": 1, "src": "VenusE-24215edb178f", "result": {"id": 0, "bat_soc": 98}}
{"id": 0, "src": "Venus-24215ee580e7", "error": {"code": -32700, "message": "Parse error", "data": 402}}
```

`src` identifies the responding device, normally as `<Model>-<ble_mac>`. It is
the most reliable free-text place the model name appears.

Error codes are the standard JSON-RPC set: -32700 parse error, -32600 invalid
request, -32601 method not found, -32602 invalid params, -32603 internal error,
and -32000 to -32099 reserved for server errors.

## 2. Discovery

Broadcast `Marstek.GetDevice` with `params.ble_mac` set to `"0"` to find every
device on the LAN. Send the same method with a real MAC to address one device.

```json
{"id": 0, "method": "Marstek.GetDevice", "params": {"ble_mac": "123456789012"}}
```

Response fields: `device` (model, e.g. `"VenusC"`), `ver` (firmware, e.g. 111),
`ble_mac`, `wifi_mac`, `wifi_name`, `ip`.

`device` is what the integration resolves capabilities from. See the model
naming section below, because firmware does not spell it consistently.

## 3. Components

### 3.1 Marstek

`Marstek.GetDevice` only. Covered above.

### 3.2 WiFi

`Wifi.GetStatus` returns `id`, `wifi_mac`, `ssid`, `rssi`, `sta_ip`,
`sta_gate`, `sta_mask`, `sta_dns`. Everything except `wifi_mac` and `rssi` may
be null.

### 3.3 Bluetooth

`BLE.GetStatus` returns `state` (`"connect"` / `"disconnect"`) and `ble_mac`.

### 3.4 Battery

`Bat.GetStatus` returns:

| Field | Type | Meaning |
| --- | --- | --- |
| `soc` | string per the table, number in practice | State of charge, % |
| `charg_flag` | boolean | Charging is currently permitted |
| `dischrg_flag` | boolean | Discharging is currently permitted |
| `bat_temp` | number or null | Battery temperature, °C |
| `bat_capacity` | number or null | Remaining capacity, Wh |
| `rated_capacity` | number or null | Rated capacity, Wh |

The two permission flags matter for passive control. A device that refuses the
requested direction is constrained, not miscalibrated.

### 3.5 PV — only for Venus A and Venus D

The heading in the PDF literally reads "3.5 PV(only for Venus A/D)". This is
the authority for gating PV in the capability table.

`PV.GetStatus` is documented with a parameter table listing `pv_power`,
`pv_voltage`, `pv_current` and `PV_state` (1 working, 0 standby). **The worked
example in the same section returns a different shape**: four numbered strings,
`pv1_power` / `pv1_voltage` / `pv1_current` / `pv1_state` through `pv4_*`.

The integration parses the unnumbered singular form. A device answering with
the numbered form will produce empty solar sensors. This is a known limitation
and was deliberately left unfixed; correcting it means deciding whether to sum
the strings or expose them separately, which is a data-model change rather than
a capability change.

### 3.6 ES (Energy System)

Three methods: `ES.GetStatus`, `ES.SetMode`, `ES.GetMode`.

#### ES.GetStatus

`id`, `bat_soc` (%), `bat_cap` (Wh), `pv_power` (W), `ongrid_power` (W),
`offgrid_power` (W), `bat_power` (W), `total_pv_energy`,
`total_grid_output_energy` (Wh), `total_grid_input_energy` (Wh),
`total_load_energy` (Wh). All may be null.

Note the unit on `total_pv_energy`: the specification says `0.01*KWh`, which is
10 Wh per count, not 1 Wh. The integration currently publishes it as Wh.
Treat any scaling change here as a breaking statistics change.

#### ES.SetMode

```json
{"id": 1, "method": "ES.SetMode", "params": {"id": 0, "config": {"mode": "...", "<mode>_cfg": {...}}}}
```

`mode` is one of `Auto`, `AI`, `Manual`, `Passive`, `Ups`. Each takes its own
config object: `auto_cfg`, `ai_cfg`, `manual_cfg`, `passive_cfg`, `ups_cfg`.

The response is `{"id": ..., "set_result": true}`. A transport-level success is
not an acknowledgement; `set_result` must be present and true. The PDF's
examples misspell this value as `ture`, which is a typo in the document, not a
wire format.

`auto_cfg` and `ai_cfg` each take only `enable: 1`. There is no "off" value;
turning a mode off means setting a different mode.

`manual_cfg`:

| Field | Type | Meaning |
| --- | --- | --- |
| `time_num` | number | Time period index. **Venus A/C/D/E: 0-9. Venus E mini: 0-5.** |
| `start_time` | string | `hh:mm` |
| `end_time` | string | `hh:mm` |
| `week_set` | number | Bitmask, low 7 bits. 1 = Monday, 3 = Mon+Tue, 127 = all week |
| `power` | number | Watts |
| `enable` | number | 1 on, 0 off |
| `manual_set` | number | **Venus E mini only.** 0 disable, 1 charge, 2 discharge, 3 auto |

One command addresses one slot. Configuring a full schedule means sending the
command once per `time_num`.

`passive_cfg` takes `power` (W) and `cd_time` (countdown, seconds). The device
reverts when the countdown expires, which is why passive control needs a
keepalive.

`ups_cfg` takes `enable`. Not implemented in this integration. Note the
inconsistency in the PDF: the config table spells the mode `"Ups"` while the
worked example sends `"UPS"`. Verify against hardware before implementing.

#### ES.GetMode

Returns `mode`, `ongrid_power`, `offgrid_power`, `bat_soc`, and then
`ct_state`, `a_power`, `b_power`, `c_power`, `total_power`, `input_energy`,
`output_energy`. **The CT-derived fields are explicitly documented as effective
only in Auto and AI modes.** `input_energy` and `output_energy` are in 0.1 Wh.

`mode` is typed "number or null" in the table but is a string in every example.

### 3.7 EM (Energy Meter / CT)

`EM.GetStatus` returns `ct_state` (0 not connected, 1 connected), `a_power`,
`b_power`, `c_power`, `total_power`, `input_energy`, `output_energy`. The two
energy counters are in 0.1 Wh here as well.

`ct_state == 0` is an explicit "no CT fitted" answer rather than a failure,
which is why the integration stops polling EM on that value specifically and
not on a timeout.

### 3.8 SYS

None of these are implemented in this integration.

| Method | Params | Purpose |
| --- | --- | --- |
| `DOD.SET` | `value` 30-88 | Depth of discharge. Default 88 |
| `Ble.Adv` | `enable` | Bluetooth advertising. **0 enables, 1 disables** |
| `Led.Ctrl` | `state` | Panel LED. 1 on, 0 off |
| `Set.Ver` | `version` | Power rating: 800, 1200 (VA), 1500 (VA), 2200 (VD), 2500 (VE) |
| `Reset.Factory` | `type` | 1 clears accumulated data, 2 keeps it |

`Ble.Adv` inverts the usual polarity. That is what the document says.

## 4. Per-model support

Chapter 4 of the PDF, verbatim in substance:

| Model | Components |
| --- | --- |
| Venus C / Venus E | Marstek, WiFi, Bluetooth, Battery, ES, EM, SYS (firmware >= 150) |
| Venus D / Venus A | Marstek, WiFi, Bluetooth, Battery, **PV**, ES, EM, SYS (firmware >= 150) |
| Venus E mini | Marstek, WiFi, Bluetooth, Battery, ES, EM, SYS (DOD, Ble_block, Led_Ctrl only) |

So exactly two capability axes are documented as varying between models:

1. PV presence. Venus A and Venus D only.
2. Manual mode. The E mini has six time periods instead of ten and accepts
   `manual_set`.

Everything else in the API is common to all five models. No model is documented
as lacking any ES mode, so the integration offers all four to every model.

This maps directly onto `custom_components/marstek/capabilities.py`. Adding a
new model should mean adding one table entry there and nothing else.

## 5. Model naming in the wild

The specification is inconsistent with itself and firmware is inconsistent with
both. Observed spellings of the same product family:

- `VenusC`, `VenusE`, `VenusD` in `Marstek.GetDevice` results
- `Venus C`, `Venus E mini` in prose
- `VenusC-123456789012`, `VenusE-24215edb178f`, `VenusD-009b08a5ac28`,
  `Venus-24215ee580e7` in `src`
- `VNSEM-0` as the SKU-style code real Venus E mini units report
- Firmware-suffixed forms such as `VenusE 3.0`

Never compare a reported model with `==`. Normalize first: strip a trailing
hex serial, lowercase, drop non-alphanumerics, then match by longest prefix so
`venusemini` and `vnsem` are not swallowed by `venuse` and `vnse`.

## 6. Things the specification does not tell you

- No rate limit is documented. Polling every 30 seconds with paced sequential
  requests is what this integration settled on.
- The device answers one request at a time. Concurrent UDP requests are not
  documented as supported.
- Passive mode's `cd_time` means the commanded power expires. Anything that
  wants sustained output must resend before the countdown ends.
- Measured output does not match commanded power on real hardware, which is
  what this integration's passive calibration exists to correct.
- `set_result: true` acknowledges receipt of the command, not that the device
  reached the requested state. Confirmation requires a separate `ES.GetMode`.
- One unconfirmed community report claims Venus E mini firmware 300 rejects
  Passive mode. The specification does not gate modes per model, so the
  capability table does not either. Revisit if it is confirmed.
