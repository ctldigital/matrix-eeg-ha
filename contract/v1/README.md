# MQTT contract v1 (platform <-> Home Assistant integration)

The JSON files in this directory are the single source of the contract. The Go platform
(`internal/drivers/haheatpump`) and the Python integration both read them in their tests;
neither side defines the shape anywhere else. Topics are relative to the device prefix `<p>` = `eeg/<username>`.

## `<p>/state` (integration -> platform)

`state_heat_pump.json` (all fields) and `state_heat_pump_minimal.json` (required fields only).

- Required: `v` (= 1), `category` (`heat_pump`), `ts`, `boost_active` (true while the integration holds a boost).
  That includes an unknown setpoint while the lease runs, and for up to 120 s after a boost write, a setpoint that still
  shows the value it replaced (the vendor entity may update only on its next poll).
- Optional, `null` = unknown: `dhw_temp_c`, `dhw_target_c`, `power_w`, `heater_rod_active`, `dhw_max_c`.
- `ts` is informational: the platform always uses the receive time.

## `<p>/cmd` (platform -> integration)

`cmd_boost.json` and `cmd_end.json`: `{"v":1,"id":<int>,"target_c":…,"fallback_c":…,"lease_s":…}`.

- Boost: `target_c` > `fallback_c`, `lease_s` > 0. The integration sets `target_c` and returns to `fallback_c`
  when the lease runs out, unless the setpoint was changed manually meanwhile.
- End: `target_c` = `fallback_c`, `lease_s` = 0.
- `id` is an integer; the integration echoes it in the ack.

## `<p>/ack` (integration -> platform)

`ack_ok.json` / `ack_error.json`: `{"id":7,"ok":true}` or `{"id":7,"ok":false,"error":"<German text>"}`.

## Invalid commands

`cmd_invalid.json` is an array of `{name, payload, error}`. The integration answers each payload with
`ok:false` and exactly `error`, and leaves the setpoint unchanged. `error: null` (`no_id`): no ack at all,
since there is no id to answer.

## Validation order of `parse_cmd`

The integration checks a command in exactly this order and answers with the first matching error:

1. Invalid JSON, not an object, or no integer `id` -> no ack at all.
2. `v` != 1 -> `Unbekannte Vertragsversion <v>`.
3. `lease_s` not an integer in 0..86400 -> `Ungültige Lease`.
4. `target_c` or `fallback_c` missing or not a number -> `Ungültiger Befehl`.
5. `target_c` or `fallback_c` above the hard cap 60 -> `Sollwert über Obergrenze 60 °C`. The local limit option
   does not reject: like the „max. Warmwassertemperatur“ entity, it clamps the written value.
6. `target_c` < `fallback_c` -> `Ungültiger Befehl`.
7. `target_c` > `fallback_c` with `lease_s` = 0 -> `Ungültige Lease`.

Booleans are never numbers or integers here.
