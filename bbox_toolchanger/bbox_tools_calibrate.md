# Nozzle Probe — Code Reference

Reference for `bbox_tools_calibrate.py` and `bt_nozzle_probe.cfg`. Update this
as the probing logic or macros change — unlike `plans/nozzle-probe-xyz-tool-offset-calibration.md`
(a snapshot of the original design decisions), this doc should stay current.

## Files

- `bbox_tools_calibrate.py` — klippy extra, registered as `[bbox_tools_calibrate]`. Ported from
  `viesturz/klipper-toolchanger`'s `tools_calibrate.py` (not NozzleAlign — that's a separate,
  hardware-only repo of CAD/STL probe designs that itself depends on klipper-toolchanger; it
  contains no calibration code of its own).
- `bt_nozzle_probe.cfg` — config section + macros that drive it.

## How the probing engine works

**One physical switch acts as three endstops.** `ProbeEndstopWrapper(config, axis)` is
instantiated three times (x, y, z) against the *same* `pin`. Each wrapper calls
`pins.allow_multi_use_pin()` so the pin can be shared, then on `klippy:mcu_identify` attaches
only the steppers for its own axis (`_handle_mcu_identify`). So "probe X" really means "home X
toward this switch," "probe Y" means "home Y toward this switch," etc. — same physical contact,
three different motion directions.

**`PrinterProbeMultiAxis.run_probe(direction, ...)`** drives a real homing move
(`phoming.probing_move`) in one of 6 directions (`x+/x-/y+/y-/z+/z-`) until the switch trips,
with multi-sample averaging/median and a tolerance-based retry (`samples`, `samples_tolerance`,
`samples_tolerance_retries`).

**Finding the center (`probe_xy` → `calibrate_xy`)**: from a starting point above the switch, it
moves `spread` mm to one side, lowers `lower_z` mm, then probes back toward center. Doing this
from both sides on both axes (`x+`, `x-`, `y+`, `y-`) and averaging the two trigger points per
axis gives the nozzle's true centerline — this is what lets you only need to be "within 1-2mm" by
eye when positioning over the probe.

**`locate_sensor`** ties it together: probe `z-` to find a rough top, find the XY center at that
height, move up there, re-probe `z-` for an accurate Z, then re-find XY center *again* at the
corrected position (second pass corrects for any cone/Z-coupling in the first estimate). Returns
`[x, y, z]` of the nozzle tip.

**Offsets don't affect the measurement.** All of the above moves through `toolhead.manual_move()`
/ `toolhead.get_position()` — Klipper's raw machine-coordinate APIs, which bypass the `gcode_move`
offset transform entirely (`SET_GCODE_OFFSET` only affects plain `G0`/`G1`). This is the same
reason `PROBE`/`BED_MESH_CALIBRATE` use these APIs: calibration needs ground truth, not coordinates
already corrected by the offset it's trying to measure. The macro's rough "move over the probe"
step (`G0 X{probe_x} Y{probe_y}`) *does* go through the offset transform, but harmlessly — it only
needs to land within `spread` of true center, and an already-calibrated offset only helps that
land closer (it's specifically designed to put different tools' nozzles at the same physical point
for the same gcode coordinate).

Because `manual_move()`/`set_position()` reposition the toolhead without telling `gcode_move`,
its tracked "last position" would otherwise go stale after every probe — which matters because
BBox's toolchange code (`bbox_toolchanger_impl.py`) reads that tracked position (`saved_x/y/z`)
right before each toolchange and, when not printing, unconditionally snaps Z back to it afterward
(`variable_restore_after_toolchange_z: 0` in `bt_base.cfg`). `locate_sensor` now calls
`self.gcode_move.reset_last_position()` right after parking, so that tracked position always
matches where the toolhead actually is — this keeps the loop correct regardless of how
`BT_CALIBRATE_TOOL_OFFSETS` is reordered later, rather than relying on the macro always issuing
a `G0 Z{travel_z}` immediately before probing.

## Commands

- **`TOOL_LOCATE_SENSOR`** (call once, T0 active): runs `locate_sensor`, stores the result as
  `self.sensor_location` — the baseline.
- **`TOOL_CALIBRATE_TOOL_OFFSET`** (call per other tool): runs `locate_sensor` again with the new
  tool loaded, subtracts the baseline → `self.last_result = [dx, dy, dz]`, exposed as
  `printer["bbox_tools_calibrate"].last_x_result/last_y_result/last_z_result`.
- **`TOOL_CALIBRATE_PROBE_OFFSET`** (dormant): compares this probe's Z reading against the main
  bed probe's (`probe:` config key) trigger point, for calibrating that probe's `z_offset`. Only
  useful once a bed probe (e.g. Klicky) is reconnected and active.
- **`TOOL_CALIBRATE_QUERY_PROBE`**: prints "open"/"TRIGGERED" — quick sanity check the switch
  isn't already pressed before starting a calibration run.

## Macros (`bt_nozzle_probe.cfg`)

- **`BT_CALIBRATE_TOOL_OFFSETS`** — moves over the probe (`variable_probe_x/y/travel_z`),
  heats to `variable_calibrate_temp`, runs `TOOL_LOCATE_SENSOR` on T0, then loops every other
  physical tool (`printer["gcode_macro _BT_VARIABLES"].x_locs|length`, not hardcoded),
  calling `TOOL_CALIBRATE_TOOL_OFFSET` then `BT_SAVE_LAST_TOOL_OFFSET` for each.
- **`BT_SAVE_LAST_TOOL_OFFSET TOOL=n`** — pulls `last_x/y/z_result` off
  `printer["bbox_tools_calibrate"]` and forwards them into the existing `BT_SAVE_TOOL_OFFSET`
  (in `bbox_toolchanger_impl.py`), which writes `tool_offsets` / `variables.cfg`. Can also be
  called standalone (`T2` → `TOOL_CALIBRATE_TOOL_OFFSET` → `BT_SAVE_LAST_TOOL_OFFSET TOOL=2`) to
  redo a single tool without the full loop, as long as Klipper hasn't restarted since T0's
  baseline was set (the baseline lives in the python object's runtime state, not persisted).
- **`BT_CALIBRATE_PROBE_OFFSET`** — wraps the dormant `TOOL_CALIBRATE_PROBE_OFFSET` command,
  reusing the same probe position variables as `BT_CALIBRATE_TOOL_OFFSETS`.

## Known follow-ups

- `variable_probe_x` / `variable_probe_y` / `variable_travel_z` on `BT_CALIBRATE_TOOL_OFFSETS`
  are placeholders carried over from upstream's example (`229, 2.5, 60`) — set these to your
  actual probe dock location before running.
- `pin: ^pico2:gpio11` reuses the MCU pin freed by disabling `bt_zprobe.cfg`'s `zprobe_pin`
  (commented out in `printer.cfg`). The polarity (`^`, no `!`) matches what was already verified
  correct for an NC switch on this pin by the old zprobe feature, but the new physical switch
  may behave differently — **verify with `TOOL_CALIBRATE_QUERY_PROBE` before running any probing
  command** (see "Testing the switch safely" below).
- The old `zprobe_pin`/`ZPROBE_TOOL_OFFSET` Z-only path and the sidecar UI's "Calibrate All"
  feature (`plans/calibrate-all-z-offsets.md`) are disabled (not removed) while this new probe is
  validated. Once confirmed working, remove `ZPROBE_TOOL_OFFSET` from `bbox_toolchanger.py` /
  `bbox_toolchanger_impl.py`, delete `bt_zprobe.cfg`, and update the sidecar UI — that's a
  separate session.

## Testing the switch safely

`TOOL_CALIBRATE_QUERY_PROBE` reads the pin's static state with **zero motion** — safe to run
any time, including before homing:

1. Deploy and restart Klipper (a pin change requires `--service-restart`, not `BT_RELOAD`).
2. With the switch untouched, run `TOOL_CALIBRATE_QUERY_PROBE` — expect `open`.
3. Manually actuate the switch by hand and run it again — expect `TRIGGERED`.
4. If the readings are backwards (says `TRIGGERED` at rest, or `open` while pressed), the
   polarity is inverted: toggle `pin: ^pico2:gpio11` to `pin: ^!pico2:gpio11` in
   `bt_nozzle_probe.cfg`, restart, and repeat steps 2-3.
5. Only once both states read correctly, move on to `TOOL_LOCATE_SENSOR` /
   `TOOL_CALIBRATE_TOOL_OFFSET`. As a backstop, Klipper's homing code refuses to start a probing
   move if the endstop already reads triggered (raises an error instead of moving), so a still-
   wrong polarity is more likely to safe-error than to crash the toolhead — but confirming first
   means you're not relying on that backstop.
