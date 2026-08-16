import time
import re
import os
import shutil
import logging

# Matches 'extruder' and 'extruder1'..'extruder99', but not heater_bed,
# chamber heaters, or any heater_generic.
_BT_EXTRUDER_RE = re.compile(r'^extruder\d*$')

# Options never copied from [extruder] down to [extruder1]..[extruderN].
#
# T1+ are heater-only extruders with no stepper of their own. Klipper only builds
# an ExtruderStepper when step_pin/dir_pin/rotation_distance is present
# (kinematics/extruder.py), so any stepper-owned option copied onto a heater-only
# section would go unread - and Klipper refuses to start on an unread option
# ("Option 'x' is not valid in section 'extruder1'"). Pins and gcode_id are
# excluded because they identify a specific tool.
#
# Note 'pressure_advance_smooth_time' is listed explicitly rather than matched as
# a *_smooth_time pattern: the heater's own 'smooth_time' is a different option
# that we do want to inherit.
_BT_NO_INHERIT = frozenset([
    # per-tool identity
    'heater_pin', 'sensor_pin', 'gcode_id',
    # stepper.PrinterStepper
    'step_pin', 'dir_pin', 'enable_pin', 'endstop_pin',
    'microsteps', 'rotation_distance', 'gear_ratio',
    'full_steps_per_rotation', 'step_pulse_duration',
    'position_endstop', 'position_min', 'position_max',
    'homing_speed', 'homing_positive_dir',
    # kinematics.extruder.ExtruderStepper
    'pressure_advance', 'pressure_advance_smooth_time',
])

class ToolLogic:
    def __init__(self, printer, config):
        self.config = config
        self.printer = printer
        self.gcode = printer.lookup_object('gcode')
        self.tool_button = printer.lookup_object('gcode_button tool_detect', None)
        self.var_macro = printer.lookup_object('gcode_macro _BT_VARIABLES')

        # Extruder config inheritance has to land before Klipper turns the
        # [extruderN] sections into objects. ToolLogic.__init__ runs from the proxy,
        # which Klipper constructs in its config-section loop (klippy.py:122) -
        # strictly earlier than toolhead.add_printer_objects() (klippy.py:125), where
        # kinematics/extruder.py scans for [extruderN] sections. So this is early
        # enough, whereas klippy:connect / klippy:ready are not.
        self._inherit_report = {}
        self._inherit_error = None
        try:
            self.apply_extruder_inheritance(config)
        except Exception as e:
            # Deliberately not fatal. A long-form config needs nothing from us, and
            # bricking Klipper startup over a bug in here would be a poor trade. A
            # trimmed config that misses out still fails loudly, via Klipper's own
            # "option must be specified" error.
            self._inherit_error = str(e)
            logging.exception("[BBox] extruder config inheritance failed")

        # Optionally set up Z probe endstop if bt_zprobe.cfg is loaded
        self._setup_zprobe(config)

        # Auto-discovery of extruder heaters for [heater_fan bt_*] must land before
        # klippy:ready, when Klipper's PrinterHeaterFan resolves heater names into
        # heater objects. klippy:connect is the last hook that still gets there in time.
        self._auto_fan_report = {}
        self.printer.register_event_handler('klippy:connect', self._handle_connect)
        self.printer.register_event_handler('klippy:ready', self._handle_ready_checks)
        # klippy:connect only fires once per Klipper start, so after a BT_RELOAD the
        # handler above never runs again. Re-apply directly here so a reloaded instance
        # still reports the mapping. Idempotent: the heater set cannot change without
        # a restart, and klippy:connect overwrites this on a cold start anyway (where
        # it may find only a partial list, depending on config section ordering).
        try:
            self.apply_auto_heater_fans()
        except Exception:
            pass

        self.show_current_state()

        try:
            self.get_z_limit(config)
            self._setup_axis_config(config)
            # Attempt to sync from disk as soon as Python loads the class
            self.sync_and_init_arrays()
        except:
            # If the printer object isn't fully built yet, we'll catch it in the first command
            pass

    def get_z_limit(self, config):
        """Retrieves Z axis endstop position and homing direction to inform safe Z lift during tool changes."""
        try:
            # Access the [stepper_z] section specifically
            z_config = config.getsection('stepper_z')
            
            # 1. Get position_endstop (required for homing axes)
            self.z_pos_endstop = z_config.getfloat('position_endstop', None)
            
            # 2. Get homing_positive_dir
            # If not explicitly in config, you may need to check Klipper's inferred logic
            self.z_homing_positive_dir = z_config.getboolean('homing_positive_dir', None)
            
            if self.z_homing_positive_dir is None:
                # Klipper's internal logic: default True if endstop is near max
                pos_max = z_config.getfloat('position_max')
                pos_min = z_config.getfloat('position_min', 0.0)
                self.z_homing_positive_dir = (abs(self.z_pos_endstop - pos_max) < 
                                    abs(self.z_pos_endstop - pos_min))

        except Exception:
            self.gcode.respond_info("[BBOX] WARNING: exception in get_z_limit()")
            pass

    def _setup_axis_config(self, config):
        """Read X/Y endstop positions, axis max, and homing speeds from config for
        dock calibration. Probe speeds derive from each axis's configured homing_speed
        so calibration adapts to the endstop type in use (physical vs sensorless):
        the user has already tuned homing_speed appropriately for that axis.
        Klipper's default homing_speed is 5.0 mm/s when unset."""
        try:
            x_cfg = config.getsection('stepper_x')
            self.x_min          = x_cfg.getfloat('position_min', 0.0)
            self.x_endstop_pos  = x_cfg.getfloat('position_endstop')
            self.x_max          = x_cfg.getfloat('position_max')
            self.x_homing_speed = x_cfg.getfloat('homing_speed', 5.0, above=0.)
        except Exception:
            self.x_min          = 0.0
            self.x_endstop_pos  = 0.0
            self.x_max          = 215.0
            self.x_homing_speed = 5.0
        try:
            y_cfg = config.getsection('stepper_y')
            self.y_min          = y_cfg.getfloat('position_min', 0.0)
            self.y_endstop_pos  = y_cfg.getfloat('position_endstop')
            self.y_max          = y_cfg.getfloat('position_max')
            self.y_homing_speed = y_cfg.getfloat('homing_speed', 5.0, above=0.)
        except Exception:
            self.y_min          = 0.0
            self.y_endstop_pos  = 0.0
            self.y_max          = 218.0
            self.y_homing_speed = 5.0

    def _setup_zprobe(self, config):
        """Optionally configure the Z probe endstop from bt_zprobe.cfg.
        bt_zprobe.cfg adds zprobe_pin (and related keys) to [bbox_toolchanger].
        If zprobe_pin is absent the feature is gracefully disabled."""
        self.zprobe_pin = None
        pin_desc = config.get('zprobe_pin', None)
        if pin_desc is None:
            return
        try:
            ppins = self.printer.lookup_object('pins')
            self.zprobe_pin = ppins.setup_pin('endstop', pin_desc)
            # Register with query_endstops so QUERY_ENDSTOPS shows the switch state
            query_endstops = self.printer.load_object(config, 'query_endstops')
            query_endstops.register_endstop(self.zprobe_pin, 'bbox_zprobe')
            self.zprobe_speed     = config.getfloat('zprobe_speed',                    3.0,   above=0.)
            self.zprobe_lift_speed = config.getfloat('zprobe_lift_speed',              8.0,   above=0.)
            self.zprobe_samples   = config.getint(  'zprobe_samples',                  3,     minval=1)
            self.zprobe_tolerance = config.getfloat('zprobe_samples_tolerance',        0.010, minval=0.)
            self.zprobe_retries      = config.getint(  'zprobe_samples_tolerance_retries', 3,    minval=0)
            self.zprobe_max_descend  = config.getfloat('zprobe_max_descend',               20.0,  above=0.)
            # Associate Z steppers with the endstop once kinematics are available.
            # Required so HomingMove knows which stepper to stop on trigger.
            self.printer.register_event_handler(
                'klippy:mcu_identify', self._handle_zprobe_mcu_identify)
            self.gcode.respond_info("[BBOX] Z probe configured on pin: {}".format(pin_desc))
        except Exception as e:
            self.gcode.respond_info("[BBOX] WARNING: Failed to configure Z probe: {}".format(e))
            self.zprobe_pin = None

    def _handle_zprobe_mcu_identify(self):
        """Associate all active Z steppers with the Z probe endstop.
        Called during klippy:mcu_identify, before klippy:connect, following
        the same pattern as Klipper's own probe module."""
        if self.zprobe_pin is None:
            return
        kin = self.printer.lookup_object('toolhead').get_kinematics()
        for stepper in kin.get_steppers():
            if stepper.is_active_axis('z'):
                self.zprobe_pin.add_stepper(stepper)
                self.gcode.respond_info(
                    "[BBOX] Z probe: registered stepper '{}'".format(stepper.get_name()))

    # ------------------------------------------------------------------
    # Extruder config inheritance
    # ------------------------------------------------------------------

    def _rebuild_inherit_report(self, config):
        """Recover which options were inherited, by diffing the live config against
        what the user actually wrote.

        status_raw_config is built inside read_main_config(), before any of our
        injection runs, and already includes SAVE_CONFIG values. So options present
        in the live fileconfig but absent from it are exactly the ones we added.
        Used on the BT_RELOAD path, where re-injecting is not an option.
        """
        raw = self._raw_config()
        fileconfig = config.fileconfig
        report = {}
        for section in fileconfig.sections():
            if not section.startswith('extruder'):
                continue
            suffix = section[len('extruder'):]
            if not suffix.isdigit():
                continue
            written = set(raw.get(section, {}))
            inherited = sorted(o for o in fileconfig.options(section)
                               if o not in written)
            if inherited:
                report[section] = inherited
        return report

    def apply_extruder_inheritance(self, config):
        """Fill each [extruderN] (N>=1) with the options it left out, copied from
        [extruder], so a new tool only has to state its own heater_pin/sensor_pin.

        Called from __init__, i.e. while Klipper is still building config-section
        objects and before any extruder exists. ConfigWrapper.has_section() and
        getsection() read the live fileconfig, so sections mutated here are seen in
        full by PrinterExtruder.

        Only options a section did NOT set are added, so explicit per-tool values
        always win - including PID values written by SAVE_CONFIG, which Klipper has
        already merged into this same fileconfig (configfile.py, load_main_config ->
        append_fileconfig '*AUTOSAVE*'). That same merge is what lets T0's tuned
        control/pid_* be inherited as a usable starting point: 'control' has no
        default in heaters.py, so without it a trimmed [extruderN] would not boot.
        """
        # BT_RELOAD re-runs __init__ at runtime. By then the extruders are built and
        # the config has been validated, so injecting is pointless and unwanted.
        # toolhead does not exist yet during the startup pass, which makes it a clean
        # discriminator between the two cases. Rebuild the report rather than leaving
        # it empty, so BT_CHECK_CONFIG keeps telling the truth after a reload.
        if self.printer.lookup_object('toolhead', None) is not None:
            try:
                self._inherit_report = self._rebuild_inherit_report(config)
            except Exception:
                logging.exception("[BBox] could not rebuild inheritance report")
            return self._inherit_report

        fileconfig = config.fileconfig
        if not fileconfig.has_section('extruder'):
            return {}

        # Read through ConfigWrapper so the option is recorded in access_tracking;
        # reading it straight off fileconfig would leave Klipper's unused-option
        # check thinking it was never used, and it would reject [bbox_toolchanger].
        excluded = config.getlist('extruder_inherit_exclude', [])
        skip = _BT_NO_INHERIT.union(o.strip().lower() for o in excluded)

        source = [(opt, fileconfig.get('extruder', opt))
                  for opt in fileconfig.options('extruder')
                  if opt.lower() not in skip]
        if not source:
            return {}

        self._inherit_report = {}
        for i in range(1, 99):
            # break, not continue: this mirrors kinematics/extruder.py, which stops
            # at the first gap. Sections past a gap are ignored by Klipper, so
            # inheriting into them would only make a broken config look healthy.
            section = 'extruder%d' % (i,)
            if not fileconfig.has_section(section):
                break
            applied = []
            for option, value in source:
                if fileconfig.has_option(section, option):
                    continue
                fileconfig.set(section, option, value)
                applied.append(option)
            if applied:
                applied.sort()
                self._inherit_report[section] = applied
                logging.info("[BBox] %s inherited %d option(s) from [extruder]: %s",
                             section, len(applied), ", ".join(applied))
        return self._inherit_report

    # ------------------------------------------------------------------
    # Heater fan auto-discovery + config validation
    # ------------------------------------------------------------------

    def _handle_connect(self):
        try:
            self.apply_auto_heater_fans()
        except Exception:
            logging.exception("[BBox] auto heater_fan discovery failed")

    def _handle_ready_checks(self):
        """klippy:ready can fire before Moonraker has finished (re)subscribing
        its webhooks client to this restart's gcode output, in which case a
        respond_info() sent directly here never reaches Moonraker (no replay
        buffer - it's simply not in the callback list yet). Defer the actual
        report a few seconds so subscription has reliably completed first."""
        reactor = self.printer.get_reactor()
        reactor.register_callback(self._report_ready_checks,
                                   reactor.monotonic() + 3.0)

    def _report_ready_checks(self, eventtime):
        """Run the config validator, reporting only when something is wrong
        so a healthy config adds no console noise to every boot."""
        try:
            problems = self.check_config()
            if problems:
                self.gcode.respond_info(
                    "[BBOX] Config warnings (run BT_CHECK_CONFIG for detail):\n"
                    + "\n".join(problems))
        except Exception:
            logging.exception("[BBox] config check failed")

    def find_extruder_heaters(self):
        """All extruder heaters Klipper actually registered, in numeric order.

        available_heaters is populated during setup_heater() at config-parse time,
        so this is complete by klippy:connect. Sorted numerically rather than left
        in config-parse order so output is deterministic and reads naturally."""
        pheaters = self.printer.lookup_object('heaters')
        found = [n for n in pheaters.available_heaters if _BT_EXTRUDER_RE.match(n)]
        return sorted(found, key=lambda n: int(n[len('extruder'):] or 0))

    def apply_auto_heater_fans(self):
        """Fill in the heater list for every [heater_fan bt_*] section.

        Klipper's PrinterHeaterFan reads `heater` into self.heater_names at config-parse
        time but does not resolve it into heater objects until klippy:ready. Rewriting
        heater_names during klippy:connect therefore reaches the fan before it binds,
        leaving all of Klipper's PWM / kick-start / timer / shutdown-speed logic intact.
        """
        found = self.find_extruder_heaters()
        self._auto_fan_report = {}
        for name, fan_obj in self.printer.lookup_objects(module='heater_fan'):
            short = name.split(' ', 1)[1] if ' ' in name else name
            if not short.startswith('bt_'):
                continue
            if not found:
                # Never assign an empty list: that would silently disable the fan,
                # which is exactly the failure mode this feature exists to remove.
                logging.warning(
                    "[BBox] %s: no extruder heaters found, leaving %s",
                    name, fan_obj.heater_names)
                continue
            fan_obj.heater_names = found
            self._auto_fan_report[short] = found
            logging.info("[BBox] %s -> %s", name, ", ".join(found))
        return self._auto_fan_report

    def _raw_config(self):
        """Raw section/option dict as the user actually wrote it, so the validator
        can tell an explicit setting apart from a Klipper default."""
        configfile = self.printer.lookup_object('configfile')
        eventtime = self.printer.get_reactor().monotonic()
        return configfile.get_status(eventtime).get('config', {})

    def check_config(self):
        """Return a list of problem strings; empty means the config looks healthy."""
        problems = []
        found = self.find_extruder_heaters()

        # 1. Tool count mismatch. The rest of the codebase derives tool count from
        #    x_locs|length at gcode runtime, so a mismatch here is a real silent failure.
        n_locs = len(self.get_var('x_locs', []))
        if n_locs and len(found) != n_locs:
            problems.append(
                "  ERROR: {} extruder heater(s) defined but x_locs has {} entr(y/ies). "
                "Every tool needs a matching [extruderN] section."
                .format(len(found), n_locs))

        if self._inherit_error is not None:
            problems.append(
                "  ERROR: extruder config inheritance failed ({}). Any [extruderN] "
                "relying on values inherited from [extruder] will be incomplete."
                .format(self._inherit_error))

        # Fresh-install sanity checks: bt_customize.cfg ships x_locs/dock_y with
        # placeholder values (10000) far outside any real machine's travel, so an
        # out-of-bounds value here is almost certainly that placeholder left
        # unedited rather than a deliberate setting.
        x_locs = self.get_var('x_locs', [])
        if not x_locs:
            problems.append(
                "  ERROR: x_locs is empty - no tool dock positions are configured. "
                "Set variable_x_locs in bt_customize.cfg.")
        else:
            for i, x in enumerate(x_locs):
                if not (self.x_min <= x <= self.x_max):
                    problems.append(
                        "  ERROR: x_locs[{}] is {}, outside the X axis travel ({} to {}). "
                        "Likely still the bt_customize.cfg placeholder - update variable_x_locs."
                        .format(i, x, self.x_min, self.x_max))

        dock_y = self.get_var('dock_y', None)
        if dock_y is not None and not (self.y_min <= dock_y <= self.y_max):
            problems.append(
                "  ERROR: dock_y is {}, outside the Y axis travel ({} to {}). "
                "Likely still the bt_customize.cfg placeholder - update variable_dock_y."
                .format(dock_y, self.y_min, self.y_max))

        if self.tool_button is None:
            problems.append(
                "  ERROR: [gcode_button tool_detect] is not configured. Tool presence "
                "will always read as 'not detected', and BT_LOAD_TOOL/BT_UNLOAD_TOOL "
                "will fail. Uncomment and configure [gcode_button tool_detect] "
                "(with its pin) in bt_customize.cfg.")

        raw = self._raw_config()
        numbered = sorted(int(s[len('extruder'):]) for s in raw
                          if s.startswith('extruder')
                          and s[len('extruder'):].isdigit())

        # 5. Gap in the extruder numbering. Klipper stops at the first gap, so any
        #    section past it is silently ignored - easy to hit now that a tool is
        #    only a couple of lines and simple to overlook when deleting one.
        if numbered:
            missing = [n for n in range(1, max(numbered) + 1) if n not in numbered]
            if missing:
                problems.append(
                    "  ERROR: [extruder{}] is defined but {} missing. Klipper "
                    "stops at the first gap, so every section after it is ignored "
                    "entirely. Renumber the tools to be contiguous."
                    .format(max(numbered),
                            ", ".join("[extruder%d]" % (n,) for n in missing)))

        # No check is needed for a tool that failed to declare its own heater_pin or
        # sensor_pin. Both are in _BT_NO_INHERIT, so a tool can never pick up T0's
        # pin and end up silently sharing it; Klipper rejects the section outright
        # at startup with "option must be specified", long before this ever runs.

        for section, options in raw.items():
            if not section.startswith('heater_fan '):
                continue
            short = section.split(' ', 1)[1]
            heater_opt = options.get('heater')
            if short.startswith('bt_'):
                # 3. Redundant heater: on a managed fan - it is being overridden.
                if heater_opt is not None:
                    problems.append(
                        "  WARNING: [{}] sets 'heater:' but is auto-managed by BBox; "
                        "that line is overridden and should be deleted."
                        .format(section))
            elif heater_opt is not None and 'extruder' in heater_opt:
                # 2. Legacy un-migrated fan - catches a rename missed on one printer.
                problems.append(
                    "  WARNING: [{}] lists extruder heaters manually and is NOT "
                    "auto-managed. Rename it to 'heater_fan bt_{}' and delete its "
                    "'heater:' line to enable auto-discovery."
                    .format(section, short))

        return problems

    def BT_CHECK_CONFIG(self, gcmd):
        """Report discovered extruders, the bt_* fan mapping, and any problems."""
        found = self.find_extruder_heaters()
        lines = ["========== BBox Config Check =========="]
        lines.append("Extruder heaters ({}): {}".format(
            len(found), ", ".join(found) if found else "none"))
        lines.append("Tools configured (x_locs): {}".format(
            len(self.get_var('x_locs', []))))

        # 4. Informational mapping, so a newly added tool can be eyeballed as picked up.
        if self._auto_fan_report:
            for fan_name, heaters in self._auto_fan_report.items():
                lines.append("Auto-managed fan [heater_fan {}] -> {}".format(
                    fan_name, ", ".join(heaters)))
        else:
            lines.append("Auto-managed fans: none "
                         "(no [heater_fan bt_*] sections found)")

        # 5. Informational: what each tool picked up from [extruder]. Answers
        #    "why does T5 have a max_temp I never typed?" without reading this file.
        #    Sections are grouped by their inherited set, which is normally identical
        #    across every tool, so this stays one line on a healthy printer.
        if self._inherit_report:
            groups = {}
            for section, options in self._inherit_report.items():
                groups.setdefault(tuple(options), []).append(section)
            lines.append("Inherited from [extruder]:")
            for options, sections in groups.items():
                sections.sort(key=lambda s: int(s[len('extruder'):] or 0))
                lines.append("  {}: {} option(s) - {}".format(
                    ", ".join(sections), len(options), ", ".join(options)))
        else:
            lines.append("Inherited from [extruder]: nothing "
                         "(every [extruderN] sets all of its own options)")

        problems = self.check_config()
        if problems:
            lines.append("---------------------------------------")
            lines.extend(problems)
        else:
            lines.append("No problems found.")
        lines.append("=======================================")
        gcmd.respond_info("\n".join(lines))

    def show_current_state(self):
        # Pull current state for the report
        curr_t = self.get_var('current_tool', -1)
        curr_t = "None" if curr_t == -1 else f"T{curr_t}"
        det_state = "DETECTED" if self.is_tool_present() else "EMPTY"
       
        # Construct the reload message
        # Using a multi-line string for a clean "Header" look in the console
        report = (
            "========================================\n"
            f"Current Tool: {curr_t}\n"
            f"Extruder Tool State: {det_state}\n"
            "========================================"
        )
        # Send to the console
        self.gcode.respond_info(report)


    def get_var(self, name, default=None):
        """Internal helper to read from _BT_VARIABLES macro."""
        return self.var_macro.variables.get(name, default)

    # def set_var(self, name, value):
    #     """Internal helper to write to _BT_VARIABLES macro."""
    #     self.gcode.run_script_from_command(
    #         f"SET_GCODE_VARIABLE MACRO=_BT_VARIABLES VARIABLE={name} VALUE={value}"
    #     )

    def set_var(self, name, value):
        # We use repr(value) to turn [0, 0, 0, 0] into "[0, 0, 0, 0]"
        # Then we wrap the whole thing in single quotes for the G-code command
        self.gcode.run_script_from_command(
            f"SET_GCODE_VARIABLE MACRO=_BT_VARIABLES VARIABLE={name} VALUE=\"{repr(value)}\""
        )

    def _parse_tool_index_list(self, gcmd, param_name, num_tools, exclude=None):
        """Parse a comma-separated tool-index gcode param, e.g. TOOLS=0,1,3.
        Returns [] if the param wasn't given. Validates each index is in
        range; if `exclude` is given, that index is silently dropped from the
        result (e.g. AUTO redundantly listing TOOL)."""
        raw = gcmd.get(param_name, None)
        if raw is None:
            return []
        raw = raw.strip('"\'')
        try:
            indices = [int(t.strip()) for t in raw.split(',')]
        except ValueError:
            raise gcmd.error(
                f"{param_name} must be a comma-separated list of integers, "
                f"e.g. {param_name}=0,1,3")
        result = []
        for idx in indices:
            if idx < 0 or idx >= num_tools:
                raise gcmd.error(
                    f"{param_name}: index {idx} is out of range (0-{num_tools - 1})")
            if exclude is not None and idx == exclude:
                continue
            result.append(idx)
        return result

    def is_tool_present(self):
        """Live check of the tool detection switch. Always reports "not present"
        if [gcode_button tool_detect] isn't configured."""
        return self.tool_button is not None and self.tool_button.last_state == 1

    def wait_for_tool_confirmed(self, expected_state, timeout=1.0, stable_time=0.2):
        """
        Waits for the tool sensor to reach and maintain expected_state.
        Crucially: This should be called AFTER the move has finished.
        """
        if True:    # XXX, currently testing if we can simply use tool_button.last_state without the additional debounce logic as Klipper should already be debouncing for us
            return self.is_tool_present() == expected_state
        else:
            start_time = time.time()
            last_change_time = time.time()
            current_stable_state = self.is_tool_present()

            while (time.time() - start_time) < timeout:
                actual_now = self.is_tool_present()
                
                # If the state flips, reset the stability timer
                if actual_now != current_stable_state:
                    current_stable_state = actual_now
                    last_change_time = time.time()
                
                # If we are in the state we want...
                if current_stable_state == expected_state:
                    # ...check if it has been steady long enough (debounce)
                    if (time.time() - last_change_time) >= stable_time:
                        return True # Success!
                
                time.sleep(0.05) # Yield to CPU
                
            return False # Timed out

    def cleanup(self):
        """Cleanup logic if you add timers or observers later."""
        pass

    def get_status(self, eventtime):
        """Status for ToolMacroProxy: per-tool color + which tool is active
        (used by Mainsail's extruder panel to color/highlight T-buttons)."""
        filament_colors = self.get_var('filament_colors', [])
        current_tool    = self.get_var('current_tool', -1)
        gate_colors = [c.lstrip('#').lower() if c else '' for c in filament_colors]
        return {'gate_color': gate_colors, 'tool': current_tool}


    # --- INITIALIZATION & SYNC ---

    def sync_and_init_arrays(self):
        sv = self._get_saved_vars()

        x_locs = self.get_var('x_locs', [])
        num_tools = len(x_locs)

        # One-time migration of legacy tc_* persisted keys -> bt_* (idempotent).
        # Re-read save_variables afterwards so the loads below see migrated values.
        if self._migrate_legacy_variables(sv, num_tools):
            sv = self._get_saved_vars()

        # --- Stats (guarded) ---
        # Only load stats if bt_total_changes is present; if the file is
        # missing or freshly cleared the save_variables dict may be empty
        # and we don't want to overwrite in-memory stats with zeros.
        if 'bt_total_changes' in sv:
            self.set_var('total_changes', sv.get('bt_total_changes', 0))
            self.set_var('success_streak', sv.get('bt_success_streak', 0))
            self.set_var('record_high', sv.get('bt_record_high', 0))
            new_usage = [sv.get(f'bt_usage_t{i}', 0) for i in range(num_tools)]
            self.set_var('tool_usage', new_usage)

        # --- Virtual map (always load) ---
        # Stored as per-slot scalars bt_map_0, bt_map_1, ...
        # Defaults to identity mapping if not yet saved.
        virtual_map = [sv.get(f'bt_map_{i}', i) for i in range(num_tools)]
        self.set_var('virtual_map', virtual_map)

        # --- Filament colors (always load) ---
        # Stored as per-slot 24-bit integers bt_color_0, ... (-1 = unset).
        filament_colors = []
        for i in range(num_tools):
            color_int = sv.get(f'bt_color_{i}', -1)
            if isinstance(color_int, int) and color_int >= 0:
                filament_colors.append(f'#{color_int:06X}')
            else:
                filament_colors.append('')
        self.set_var('filament_colors', filament_colors)

        # --- Gate metadata ---
        gate_material = [str(sv.get(f'bt_material_{i}', '')) for i in range(num_tools)]
        gate_name     = [str(sv.get(f'bt_name_{i}', ''))     for i in range(num_tools)]
        gate_temp     = [int(sv.get(f'bt_temp_{i}', 0) or 0)              for i in range(num_tools)]
        self.set_var('gate_material', gate_material)
        self.set_var('gate_name',     gate_name)
        self.set_var('gate_temp',     gate_temp)

        # --- Tool offsets ---
        # Stored as per-slot scalars bt_xoffset_N, bt_yoffset_N, bt_zoffset_N in save_variables.
        # save_variables is the single source of truth; tools with no saved entry default to [0, 0, 0].
        new_offsets = [[0.0, 0.0, 0.0] for _ in range(num_tools)]
        for i in range(num_tools):
            xkey, ykey, zkey = f'bt_xoffset_{i}', f'bt_yoffset_{i}', f'bt_zoffset_{i}'
            if xkey in sv or ykey in sv or zkey in sv:
                new_offsets[i] = [
                    float(sv.get(xkey, 0.0)),
                    float(sv.get(ykey, 0.0)),
                    float(sv.get(zkey, 0.0)),
                ]
        self.set_var('tool_offsets', new_offsets)

    # --- STATISTICS & LOGGING ---

    def _migrate_legacy_variables(self, sv, num_tools):
        """Copy any legacy tc_* save_variables to their bt_* equivalents.

        The BT naming rename changed the persisted save_variables keys from the
        tc_ prefix to bt_. Existing printers still hold data (stats, tool map,
        colors, gate metadata, and calibrated x/y/z offsets) under the old tc_*
        keys. This runs once at load: for each key, if the old tc_* value is
        present and the new bt_* key is absent, it re-saves the value under bt_*.
        Old keys are left in place so the migration is safe to re-run.

        Returns True if at least one key was migrated (caller re-reads sv).
        """
        # Scalar keys (bare old -> new base name)
        scalar_bases = ['total_changes', 'success_streak', 'record_high']
        # Per-slot indexed families, keyed 0..num_tools-1
        indexed_bases = ['usage_t', 'map_', 'color_',
                         'material_', 'name_', 'temp_',
                         'xoffset_', 'yoffset_', 'zoffset_']

        migrated = False

        def migrate_one(base, suffix=''):
            nonlocal migrated
            old_key = f'tc_{base}{suffix}'
            new_key = f'bt_{base}{suffix}'
            if old_key in sv and new_key not in sv:
                val = sv[old_key]
                # Strings must round-trip through SAVE_VARIABLE quoted (materials/names).
                val_repr = f"'\"{val}\"'" if isinstance(val, str) else val
                self.gcode.run_script_from_command(
                    f"SAVE_VARIABLE VARIABLE={new_key} VALUE={val_repr}")
                migrated = True

        for base in scalar_bases:
            migrate_one(base)
        for base in indexed_bases:
            for i in range(num_tools):
                migrate_one(base, i)

        return migrated

    def _get_saved_vars(self):
        """The 'Skeleton Key' that finally worked."""
        try:
            sv_obj = self.printer.lookup_object('save_variables', None)
            if sv_obj:
                # Returns the confirmed dictionary seen by SEARCH_VARS
                return sv_obj.get_status(None).get('variables', {})
        except:
            pass
        return {}



    SAVE_INTERVAL = 10 # Only write to disk every 10 changes during tests

    def _flush_stats_to_disk(self):
        """Write current in-memory stats to disk."""
        total  = self.get_var('total_changes', 0)
        streak = self.get_var('success_streak', 0)
        record = self.get_var('record_high', 0)
        usage  = self.get_var('tool_usage', [])
        self.gcode.run_script_from_command(f"SAVE_VARIABLE VARIABLE=bt_total_changes VALUE={total}")
        self.gcode.run_script_from_command(f"SAVE_VARIABLE VARIABLE=bt_success_streak VALUE={streak}")
        self.gcode.run_script_from_command(f"SAVE_VARIABLE VARIABLE=bt_record_high VALUE={record}")
        for i, count in enumerate(usage):
            self.gcode.run_script_from_command(f"SAVE_VARIABLE VARIABLE=bt_usage_t{i} VALUE={count}")

    def BT_FLUSH_STATS(self, gcmd):
        """Flush deferred toolchange stats to disk. Called at print end, cancel, and pause."""
        self._flush_stats_to_disk()
        gcmd.respond_info("[BBOX] Stats flushed to disk.")

    def _record_success(self, tool_index, force_save=True):
        """Increments counts in RAM; defers disk writes to print end when printing."""
        printing = self.is_printing()

        if printing:
            # Read from RAM (already current) — no disk I/O on the hot path
            new_total  = self.get_var('total_changes', 0) + 1
            new_streak = self.get_var('success_streak', 0) + 1
            new_record = max(new_streak, self.get_var('record_high', 0))
            usage      = list(self.get_var('tool_usage', []))
            new_tool_count = (usage[tool_index] if tool_index < len(usage) else 0) + 1
            new_streak_printing = self.get_var('success_streak_printing', 0) + 1
        else:
            sv = self._get_saved_vars()
            new_total      = sv.get('bt_total_changes', 0) + 1
            new_streak     = sv.get('bt_success_streak', 0) + 1
            new_record     = max(new_streak, sv.get('bt_record_high', 0))
            new_tool_count = sv.get(f'bt_usage_t{tool_index}', 0) + 1
            new_streak_printing = 0

        # Update RAM — always, so UI stays current
        self.set_var('total_changes', new_total)
        self.set_var('success_streak', new_streak)
        self.set_var('record_high', new_record)
        self.set_var('success_streak_printing', new_streak_printing)
        usage = list(self.get_var('tool_usage', []))
        if tool_index < len(usage):
            usage[tool_index] = new_tool_count
            self.set_var('tool_usage', usage)

        # Disk writes: skipped during printing — BT_FLUSH_STATS is called at print end/pause/cancel
        if not printing:
            is_milestone = (new_total % self.SAVE_INTERVAL == 0)
            is_new_record = (new_streak == new_record)
            if force_save or is_milestone or is_new_record:
                self.gcode.run_script_from_command(f"SAVE_VARIABLE VARIABLE=bt_total_changes VALUE={new_total}")
                self.gcode.run_script_from_command(f"SAVE_VARIABLE VARIABLE=bt_success_streak VALUE={new_streak}")
                self.gcode.run_script_from_command(f"SAVE_VARIABLE VARIABLE=bt_record_high VALUE={new_record}")
                self.gcode.run_script_from_command(f"SAVE_VARIABLE VARIABLE=bt_usage_t{tool_index} VALUE={new_tool_count}")

    def _reset_streak(self):
        self.set_var('success_streak', 0)
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=bt_success_streak VALUE=0")

    def BT_STATS(self, gcmd):
        """Display toolchanger stats. Reads from RAM so values are current even mid-print."""
        total  = self.get_var('total_changes', 0)
        streak = self.get_var('success_streak', 0)
        record = self.get_var('record_high', 0)
        success_streak_printing = self.get_var('success_streak_printing', 0)
        usage  = self.get_var('tool_usage', [])
        x_locs = self.get_var('x_locs', [])

        lines = [
            "\n📊 --- BBOX TOOLCHANGER STATS ---",
            f"Lifetime Changes: {total}",
            f"Current Streak:   {streak} ✅",
            f"Record High:      {record} 🏆",
            f"Printing Streak:  {success_streak_printing}/{self.get_var('expected_changes', 0)} 🖨️",
            "\nUsage Distribution:"
        ]
        for i in range(len(x_locs)):
            count = usage[i] if i < len(usage) else 0
            pct = (count / total * 100) if total > 0 else 0
            lines.append(f"  T{i}: {count:4} times ({pct:2.0f}%)")
        lines.append("----------------------------------")
        gcmd.respond_info("\n".join(lines))

        # Also display current tool and extruder open state
        self.show_current_state();


    # --- CORE TOOLCHANGE LOGIC ---

    def BT_UNLOAD_TOOL(self, gcmd):
        """
        Unloads the currently attached tool to its dock, or the dock corresponding to parameter TOOL
        """
        tool_index = gcmd.get_int('TOOL', -1)
        self._BT_UNLOAD_TOOL(gcmd, tool_index)

    def _BT_UNLOAD_TOOL(self, gcmd, tool_index = -1):
        if tool_index == -1:
            tool_index = self.get_var('current_tool', -1)
            if tool_index == -1:
                gcmd.respond_info("No current tool to unload")

        if tool_index != -1:
            x_locs = self.get_var('x_locs', [])
            if tool_index < 0 or tool_index >= len(x_locs):
                raise gcmd.error(f"Invalid tool index: {tool_index}")

            # Check current physical state against logic state
            if not self.is_tool_present():
                raise gcmd.error("Unload failure: Sensor does not detect a tool to unload!")
        
#            gcmd.respond_info(f"Unloading tool to docking bay {tool_index}...")
            self.execute_move_and_wait(f"_BT_DO_UNLOAD_TOOL TOOL={tool_index}")
#            gcmd.respond_info("Performing wait/confirm test that tool has detached from toolhead")

        if not self.wait_for_tool_confirmed(False):
            self._reset_streak()
            gcmd.respond_info(f"❌ FAILURE: Physical tool T{tool_index} unload failed!")
            return -1

        self.set_var('current_tool', -1)

    def BT_LOAD_TOOL(self, gcmd):
        """
        Loads the tool specified by parameter TOOL
        """
        tool_index = gcmd.get_int('TOOL', -1)
        self._BT_LOAD_TOOL(gcmd, tool_index)

    def _BT_LOAD_TOOL(self, gcmd, tool_index):
        if tool_index == -1:
            gcmd.respond_info(f"TOOL must be specified")
            return

        x_locs = self.get_var('x_locs', [])
        if tool_index < 0 or tool_index >= len(x_locs):
            raise gcmd.error(f"Invalid tool index: {tool_index}")

        # Make sure there isn't already a tool attached to the extruder
        if self.is_tool_present():
            raise gcmd.error(f"Load Failure: Can not load tool while an existing tool is currently attached!")
    
#        gcmd.respond_info(f"Loading Tool {tool_index}...")
        self.execute_move_and_wait(f"_BT_DO_LOAD_TOOL TOOL={tool_index}")

        # Confirm tool has attached
#        gcmd.respond_info("Performing wait/confirm test that tool has attached to toolhead")
        if not self.wait_for_tool_confirmed(True):
            self._reset_streak()
            gcmd.respond_info(f"❌ FAILURE: Physical tool T{tool_index} load failed!")
            return -1

        # Update our internal state for the current tool
        self.set_var('current_tool', tool_index)

    def BT_CHANGE_TOOL(self, gcmd):
        # --- Virtual → physical resolution (entry point; all helpers below use physical only) ---
        virtual_tool = gcmd.get_int('TOOL', -1)
        if virtual_tool == -1:
            physical_tool = -1
        else:
            virtual_map = self.get_var('virtual_map', list(range(len(self.get_var('x_locs', [])))))
            if virtual_tool < 0 or virtual_tool >= len(virtual_map):
                raise gcmd.error(f"BT_CHANGE_TOOL: Invalid virtual tool index: {virtual_tool}")
            physical_tool = virtual_map[virtual_tool]

        travel_spd = self.get_var('travel_spd', 9000)
        current_tool = self.get_var('current_tool', -1)

        # Skip if already on the requested tool
        if physical_tool == current_tool and physical_tool != -1:
            return

        # Step 1: fan save + M107 (no Z logic here — handled below)
        self.gcode.run_script_from_command('_BEFORE_TOOLCHANGE')

        # Step 2: save gcode state (position, active offsets, mode, feed rate)
        self.gcode.run_script_from_command('SAVE_GCODE_STATE NAME=_PRE_TOOLCHANGE')

        # Step 3: capture gcode X/Y/Z before any movement (in current tool's offset space)
        gcode_move = self.printer.lookup_object('gcode_move')
        gcode_pos = gcode_move.get_status(None)['gcode_position']
        saved_x = gcode_pos.x
        saved_y = gcode_pos.y
        saved_z = gcode_pos.z

        # Capture baby-step delta: the live Z gcode offset minus the current tool's
        # calibrated Z offset. This is any user adjustment (Mainsail baby-step buttons,
        # SET_GCODE_OFFSET Z_ADJUST, etc.) that sits on top of the per-tool calibration.
        # We re-apply it after loading the new tool so it survives the tool change.
        current_z_homing = gcode_move.get_status(None)['homing_origin'].z
        current_tool_z = 0.0
        if current_tool >= 0:
            _offsets = self.get_var('tool_offsets', [])
            if current_tool < len(_offsets):
                current_tool_z = _offsets[current_tool][2]
        z_baby_step = round(current_z_homing - current_tool_z, 4)
        self.set_var('z_baby_step', z_baby_step)

        # Step 4: safety Z lift — clamped to printer Z max so we don't error at high Z
        toolhead = self.printer.lookup_object('toolhead')
        cur_physical_z = toolhead.get_position()[2]
        eventtime = self.printer.get_reactor().monotonic()
        z_max = toolhead.get_status(eventtime)['axis_maximum'].z
        #self.gcode.respond_info(f"[BBOX] WARNING: axis_maximum == {z_max}")

        saved_accel = toolhead.get_status(eventtime)['max_accel']
        accel_tc = self.get_var('accel_toolchange', 10000)
        self.gcode.run_script_from_command(f'SET_VELOCITY_LIMIT ACCEL={accel_tc}')

        try:
            # If Z homes toward maximum (endstop at top), the endstop trigger position
            # is the true physical ceiling — more reliable than axis_maximum which may
            # be configured higher than where the endstop actually fires.
            if self.z_homing_positive_dir and self.z_pos_endstop is not None:
                z_max = min(z_max, self.z_pos_endstop)
                #self.gcode.respond_info(f"[BBOX] WARNING: homing_positive_dir is True, using position_endstop {self.z_pos_endstop} as Z max")

            # Toolchange is likely over the print itself, so we want to lift above the print. 3mm is very safe but
            # takes time. After the toolchange and right be fore we resume the print I can observe the time it takes to lower back down 
            # and believe this gives an opportunity for slight ooze to come out of the nozzle before it has a chance to be pushed out onto
            # the prime tower. So I've made this value configurable (z_safety_height) with a default value 0.2mm
            z_safety_height = self.get_var('z_safety_height', 0.2)
            safe_lift = min(z_safety_height, z_max - cur_physical_z - 0.5)
            if safe_lift > 0.01:
                self.execute_move_and_wait(f'G91\nG1 Z{safe_lift:.3f} F{travel_spd}\nG90')

            # Step 5: zero offsets so all dock movements use raw absolute coordinates
            self.gcode.run_script_from_command('SET_GCODE_OFFSET X=0 Y=0 Z=0 MOVE=0')

            # Step 6: Removed as it is now actually implemented by _BT_DO_UNLOAD_TOOL which is executed a part of the _BT_CHANGE_TOOL

            # Step 7: physical unload + load (dock movements, gear open/close, sensor checks)
            if self._BT_CHANGE_TOOL(gcmd, physical_tool, allow_retry = self.is_printing()) != None:
                # An unload or load failed during the toolchange. If we are mid-print, stop everything
                # then gracefully return so the user can fix the issue and resume. If we aren't printing, just raise an error.
                if self.is_printing():
                    if self.printer.lookup_object('pause_resume'):
                        gcmd.respond_info(f"❌ FAILURE: Change to virtual T{virtual_tool} (physical T{physical_tool}) failed! Print will be paused.")
                        self.gcode.run_script_from_command('PAUSE')
                    else:
                        # Inform the user which tool failed
                        gcmd.respond_info(f"❌ FAILURE: Change to virtual T{virtual_tool} (physical T{physical_tool}) failed! Print will be aborted.")
                        self.gcode.run_script_from_command('_BT_ABORT_PRINT')
                    return -1
                else:
                    raise gcmd.error(f"❌ FAILURE: Change to virtual T{virtual_tool} (physical T{physical_tool}) failed!")

            # Step 8: post-change state restore + new tool offsets + position restore
            if physical_tool >= 0:
                # Restore gcode mode/speed; also restores old offsets (overridden immediately below)
                self.gcode.run_script_from_command(
                    'RESTORE_GCODE_STATE NAME=_PRE_TOOLCHANGE MOVE=0'
                )
                tool_offsets = self.get_var('tool_offsets', [])
                ox, oy, oz = tool_offsets[physical_tool] if physical_tool < len(tool_offsets) else (0, 0, 0)
                self.gcode.run_script_from_command(
                    f'SET_GCODE_OFFSET X={ox} Y={oy} Z={oz + z_baby_step} MOVE=0'
                )

                # Restore X/Y/Z as needed
                restore_x = self.get_var('restore_after_toolchange_x', 0)
                restore_y = self.get_var('restore_after_toolchange_y', 0)
                restore_z = self.get_var('restore_after_toolchange_z', 0)

                if restore_x or restore_y or restore_z:
                    move_cmds = ['G90']
                    if restore_x and restore_y:
                        move_cmds.append(f'G1 X{saved_x:.3f} Y{saved_y:.3f} F{travel_spd}')
                    elif restore_x:
                        move_cmds.append(f'G1 X{saved_x:.3f} F{travel_spd}')
                    elif restore_y:
                        move_cmds.append(f'G1 Y{saved_y:.3f} F{travel_spd}')
                    if restore_z:
                        # Add a small extra Z lift if printing as this position is likely on the
                        # prime tower and when resuming the print we don't want to drag
                        # against the top of a wobbly prime tower.
                        extra_lift = 0.2 if self.is_printing() else 0.0
                        move_cmds.append(f'G1 Z{saved_z + extra_lift:.3f} F{travel_spd}')
                    self.execute_move_and_wait('\n'.join(move_cmds))


                # Return to original gcode Z if restore_z didn't already handle it
                # AND we are NOT printing. If we are printing then Z will automatically
                # be handled by the print job. Otherwise we should restore Z as the
                # toolchange was a diagnostic or user-initiated action and we want to return 
                # to the original Z height after the toolchange completes.
                if not restore_z and not self.is_printing():
#                    self.execute_move_and_wait(f'G90\nG1 Z{saved_z:.3f} F{travel_spd}')
                    # Using run_script_from_command instead of execute_move_and_wait as we don't need to wait for the move to finish
                    self.gcode.run_script_from_command(f'G90\nG1 Z{saved_z:.3f} F{travel_spd}')

            else:
                # Unload-only: restore state and zero offsets
                self.gcode.run_script_from_command(
                    'RESTORE_GCODE_STATE NAME=_PRE_TOOLCHANGE MOVE=0'
                )
                self.gcode.run_script_from_command('SET_GCODE_OFFSET X=0 Y=0 Z=0 MOVE=0')

        finally:
            self.gcode.run_script_from_command(f'SET_VELOCITY_LIMIT ACCEL={saved_accel}')

        # Step 9: fan restore
        self.gcode.run_script_from_command('_AFTER_TOOLCHANGE')

        # Step 10: record success
        if physical_tool >= 0:
            self._record_success(physical_tool, force_save=True)

        # Enable the following if you want Klipper to actually honor the active extruder for each tool, 
        # which would allow Mainsail's extruder selection dropdown and console T commands to reflect the current tool. 
        # This is a bit more invasive as it requires syncing the physical tool index with Klipper's internal active extruder state, 
        # but it is doable. For now we are leaving it disabled as it doesn't add much value and could potentially cause confusion 
        # if there are any edge cases where the sync doesn't work correctly.
        if False:
            # Update Klipper's internal state for the current tool so that it's accurate for the console and any future commands
            extruder = "extruder" if physical_tool <= 0 else f"extruder{physical_tool}"

            # 1. Clear the physical motor away from any virtual motion queues
            self.gcode.run_script_from_command('SYNC_EXTRUDER_MOTION EXTRUDER=extruder MOTION_QUEUE=""')
            # 2. Update the active extruder to be the primary extruder so that Klipper and Mainsail's internal state is correct
            self.gcode.run_script_from_command(f'ACTIVATE_EXTRUDER EXTRUDER={extruder}')
            # 3. Explicitly lock the stepper back to the native extruder queue
            self.gcode.run_script_from_command(f'SYNC_EXTRUDER_MOTION STEPPER=extruder EXTRUDER=extruder MOTION_QUEUE={extruder}')

    # Unloads current tool if one is active, then loads the tool
    # specified by 'new_tool' (if it isn't -1). Returns -1 on unload, -2 on load failure, None otherwise
    # If 'allow_retry' is True, will perform one retry of the failed step (home Y/X, close extruder, then retry the step that failed)
    def _BT_CHANGE_TOOL(self, gcmd, new_tool, allow_retry = False):
        current_tool = self.get_var('current_tool', -1)
        x_locs = self.get_var('x_locs', [])

        # if specified tool is -1 then this may be a request to unload the current tool
        # without switching to an new tool. This is why the following range check uses "< -1"
        if new_tool < -1 or new_tool >= len(x_locs):
            raise gcmd.error(f"Invalid tool index: {new_tool}")

        if current_tool != -1:
            if self._BT_UNLOAD_TOOL(gcmd, current_tool) == -1:
                if allow_retry:
                    gcmd.respond_info(f"❌ FAILURE: Unload of physical T{current_tool} failed! Will perform 1 retry.")
                    # Home Y and X and close extruder
                    self.gcode.run_script_from_command('G28 Y X')
                    # Then retry the unload step
                    if self._BT_UNLOAD_TOOL(gcmd, current_tool) == -1:
                        gcmd.respond_info(f"❌ FAILURE: Retry unload of physical T{current_tool} failed!")
                        return -1
                    else:
                        gcmd.respond_info(f"✓ Toolchanger: Retry unload of physical T{current_tool} succeeded!")
                else:
                    return -1

        if new_tool != -1:
            if self._BT_LOAD_TOOL(gcmd, new_tool) == -1:
                if allow_retry:
                    gcmd.respond_info(f"❌ FAILURE: Load of physical T{new_tool} failed! Will perform 1 retry.")
                    # Home Y and X and close extruder
                    self.gcode.run_script_from_command('G28 Y X')
                    # Then retry the load step
                    if self._BT_LOAD_TOOL(gcmd, new_tool) == -1:
                        gcmd.respond_info(f"❌ FAILURE: Retry load of physical T{new_tool} failed!")
                        return -1
                    else:
                        gcmd.respond_info(f"✓ Toolchanger: Retry load of physical T{new_tool} succeeded!")
                else:
                    return -2

    def BT_FORCE_TOOL_STATE(self, gcmd):
            """
            Manually overrides the internal state.
            Usage: BT_FORCE_TOOL_STATE Tv=1  or  BT_FORCE_TOOL_STATE T=-1 (for empty)
            """
            new_t = gcmd.get_int('T', -2) # Default to -2 to detect if T was provided
            
            if new_t == -2:
                raise gcmd.error("BT_FORCE_TOOL_STATE: You must provide a T parameter (e.g., T=-1 for empty)")

            # Optional: Safety check. If user says T=1, but sensor is empty, warn them.
            if new_t != -1 and not self.is_tool_present():
                gcmd.respond_info("!!! WARNING: Syncing to T{}, but sensor is NOT triggered!".format(new_t))
            
            if new_t == -1 and self.is_tool_present():
                gcmd.respond_info("!!! WARNING: Syncing to EMPTY, but sensor IS triggered!")

            self.set_var('current_tool', new_t)
            gcmd.respond_info("✓ Toolchanger state manually synced to T{}".format(new_t))

    def BT_TEST_TOOL(self, gcmd):
        """
        Tests load/unloading of the specified tool (TOOL) for the specified number of times (COUNT)
        """
        count = gcmd.get_int('COUNT', 1)
        tool_index = gcmd.get_int('TOOL', -1)
        x_locs = self.get_var('x_locs', [])
        if tool_index < 0 or tool_index >= len(x_locs):
            raise gcmd.error(f"Invalid tool index: {tool_index}")

        tally = 0
        for i in range(count):
            gcmd.respond_info(f"Loading tool T{tool_index} {i+1}/{count}...")
            if self._BT_CHANGE_TOOL(gcmd, tool_index) != None:
                gcmd.respond_info(f"❌ FAILURE: Physical tool T{tool_index} load failed!")
                break
            gcmd.respond_info(f"unloading tool T{tool_index} {i+1}/{count}...")
            if self._BT_CHANGE_TOOL(gcmd, tool_index) != None:
                gcmd.respond_info(f"❌ FAILURE: Physical tool T{tool_index} unload failed!")
                break
        self._BT_UNLOAD_TOOL(gcmd, -1)  

    def BT_TEST_CYCLE(self, gcmd):
        """
        Cycles through tools COUNT number of times. If SQUARE is provided performs a trace square at the specified speed after each tool change.
        TOOLS optionally restricts which tools are tested and in what order (comma-separated indices).
        Usage: BT_TEST_CYCLE COUNT=5 SQUARE=6000 TOOLS=0,1,3
        """
        trace_square = gcmd.get_int('SQUARE', -1)
        if trace_square > 0:
            trace_square = max(6000, min(20000, trace_square))

        count = gcmd.get_int('COUNT', 1)
        x_locs = self.get_var('x_locs', [])
        num_tools = len(x_locs)

        tools_to_test = self._parse_tool_index_list(gcmd, 'TOOLS', num_tools)
        if not tools_to_test:
            tools_to_test = list(range(num_tools))

        tools_label = ','.join(str(t) for t in tools_to_test)
        gcmd.respond_info(f"🚀 Starting Stress Test: {count} cycles through tools [{tools_label}].")
        totalChanges = count * len(tools_to_test)
        gcmd.respond_info(f"Total tool changes to be performed: {totalChanges}")

        tally = 0
        for i in range(count):
            gcmd.respond_info(f"--- Starting Cycle {i+1}/{count} ---")

            for t_index in tools_to_test:
                tally += 1
                gcmd.respond_info(f"Changing to tool T{t_index} {tally}/{totalChanges}...")

                # Use _BT_CHANGE_TOOL to perform the actual tool change. It returns -1 if the unload step failed, -2 if the load step failed, 
                # and None if successful. We treat any non-None return as a failure and halt the test immediately to avoid further stress on the hardware.
                # If _BT_CHANGE_TOOL raises gcmd.error, this loop stops IMMEDIATELY and the printer halts.
                if self._BT_CHANGE_TOOL(gcmd, t_index) != None:
                    gcmd.respond_info(f"❌ FAILURE: Physical tool T{t_index} change failed! Testing halted at cycle {i+1}, tool T{t_index}.")
                    return

                # Use the internal record but don't force disk writes every time
                self._record_success(t_index, force_save=False)

                if trace_square > 0:
                    self.gcode.run_script_from_command(
                        f'_TRACE_SQUARE F={trace_square}'
                    )

        self._BT_UNLOAD_TOOL(gcmd, -1)
        gcmd.respond_info("✅ Stress test complete! All cycles successful.")        

    def BT_SET_GATE_MAP(self, gcmd):
        import re, json
        # Klipper uses shlex (posix=True) which strips all quotes before the handler
        # sees the params. We bypass this by reading the raw command string and
        # extracting MAP ourselves so that the JSON arrives intact.
        raw = gcmd.get_raw_command_parameters()
        m = re.search(r'MAP\s*=\s*(\{.*\})', raw, re.IGNORECASE | re.DOTALL)
        map_str = m.group(1).strip() if m else '{}'
        quiet   = gcmd.get_int('QUIET', 0)
        try:
            gate_map = {int(k): v for k, v in json.loads(map_str).items()}
        except Exception as e:
            gcmd.respond_info(f"[BBox] BT_SET_GATE_MAP parse error: {e}")
            return

        num_tools = len(self.get_var('x_locs', []))
        colors    = list(self.get_var('filament_colors', [''] * num_tools))
        materials = list(self.get_var('gate_material',   [''] * num_tools))
        names     = list(self.get_var('gate_name',       [''] * num_tools))
        temps     = list(self.get_var('gate_temp',       [0]  * num_tools))

        for gate_idx, data in gate_map.items():
            i = int(gate_idx)
            if i < 0 or i >= num_tools:
                continue
            raw_color = str(data.get('color', ''))
            colors[i]    = ('#' + raw_color[:6].lower()) if len(raw_color) >= 6 else ''
            materials[i] = str(data.get('material', ''))
            names[i]     = str(data.get('name', ''))
            temps[i]     = int(data.get('temp', 0) or 0)

        self.set_var('filament_colors', colors)
        self.set_var('gate_material',   materials)
        self.set_var('gate_name',       names)
        self.set_var('gate_temp',       temps)

        for i in range(num_tools):
            color_str = colors[i]
            color_int = int(color_str.lstrip('#'), 16) if color_str else -1
            self.gcode.run_script_from_command(f'SAVE_VARIABLE VARIABLE=bt_color_{i} VALUE={color_int}')
            self.gcode.run_script_from_command(f'SAVE_VARIABLE VARIABLE=bt_material_{i} VALUE=\'"{materials[i]}"\'')
            self.gcode.run_script_from_command(f'SAVE_VARIABLE VARIABLE=bt_name_{i} VALUE=\'"{names[i]}"\'')
            self.gcode.run_script_from_command(f'SAVE_VARIABLE VARIABLE=bt_temp_{i} VALUE={temps[i]}')

        if not quiet:
            gcmd.respond_info(f"[BBox] Gate map updated for {num_tools} gates")

    def BT_SET_TOOL_MAP(self, gcmd):
        map_str   = gcmd.get('MAP', '')
        quiet     = gcmd.get_int('QUIET', 0)
        num_tools = len(self.get_var('x_locs', []))
        try:
            indices = [int(x.strip()) for x in map_str.split(',')]
        except Exception as e:
            gcmd.respond_info(f"[BBox] BT_SET_TOOL_MAP parse error: {e}")
            return

        virtual_map = indices[:num_tools]
        while len(virtual_map) < num_tools:
            virtual_map.append(len(virtual_map))

        self.set_var('virtual_map', virtual_map)
        for i, phys in enumerate(virtual_map):
            self.gcode.run_script_from_command(f'SAVE_VARIABLE VARIABLE=bt_map_{i} VALUE={phys}')

        if not quiet:
            gcmd.respond_info(f"[BBox] Tool map updated: {virtual_map}")

    def _set_position_homed(self, toolhead, newpos):
        """Set the toolhead position and mark X/Y/Z as homed.

        Handles Klipper's API change: newer versions expect homing_axes as a string
        ("xyz"), older versions as a tuple of indices ((0, 1, 2)). Passing the wrong
        type silently fails to set the kinematic limits (no error, axes stay unhomed),
        so we try the modern string form first and fall back to the tuple.
        """
        toolhead.get_last_move_time()
        try:
            toolhead.set_position(newpos, homing_axes="xyz")
        except (TypeError, ValueError):
            toolhead.set_position(newpos, homing_axes=(0, 1, 2))

    def _bt_customize_path(self):
        """Path to the printer's bt_config/bt_customize.cfg, derived from the
        running printer.cfg location (Klipper's start_args)."""
        printer_cfg = self.printer.get_start_args()['config_file']
        config_dir = os.path.dirname(printer_cfg)
        return os.path.join(config_dir, 'bt_config', 'bt_customize.cfg')

    def _replace_customize_var(self, lines, var_name, new_value_str):
        """Replace the value on the last uncommented `variable_<var_name>:`
        line in `lines` (a list of raw lines, modified in place), preserving
        indentation and any trailing `# comment`. Returns True if a line was
        found and replaced, False otherwise."""
        key_re = re.compile(r'^(\s*variable_%s\s*:\s*)' % re.escape(var_name))
        last_idx = None
        for i, raw in enumerate(lines):
            if raw.lstrip().startswith('#'):
                continue
            if key_re.match(raw):
                last_idx = i
        if last_idx is None:
            return False

        raw = lines[last_idx]
        newline = '\n' if raw.endswith('\n') else ''
        line = raw[:len(raw) - len(newline)] if newline else raw
        m = key_re.match(line)
        prefix = m.group(1)
        rest = line[len(prefix):]
        comment_idx = rest.find('#')
        trailing = rest[comment_idx:] if comment_idx != -1 else ''

        new_line = f"{prefix}{new_value_str}"
        if trailing:
            new_line += f"  {trailing}"
        lines[last_idx] = new_line + newline
        return True

    def _write_customize_updates(self, updates):
        """Rewrite bt_customize.cfg with new values for the given
        {var_name: value_str} entries, replacing the last uncommented
        `variable_<name>:` line for each. Backs up the original file first
        and writes atomically. Returns the backup path. Raises a plain
        Exception (caught by the proxy layer and reported to the console)
        on any failure."""
        path = self._bt_customize_path()
        if not os.path.isfile(path):
            raise Exception(f"bt_customize.cfg not found at {path}")

        with open(path, 'r') as f:
            lines = f.readlines()

        for var_name, value_str in updates.items():
            if not self._replace_customize_var(lines, var_name, value_str):
                raise Exception(
                    f"No uncommented 'variable_{var_name}:' line found in {path}; "
                    f"add one manually before calibrating.")

        backup_path = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, backup_path)

        tmp_path = f"{path}.tmp-{os.getpid()}"
        with open(tmp_path, 'w') as f:
            f.writelines(lines)
        os.replace(tmp_path, path)
        return backup_path

    def BT_CALIBRATE_DOCK(self, gcmd):
        """
        Capture dock bay position (X per-tool, Y global) by probing both endstops
        after manually positioning the carriage with motors off (M84).

        Workflow:
          1. M84                          (disable motors)
          2. Push carriage to dock bay N  (physically position it)
          3. BT_CALIBRATE_DOCK TOOL=N   (runs this command)

        Uses SET_KINEMATIC_POSITION to establish a fake coordinate frame at
        position_max (within Klipper's valid range), then probes Y first (moves
        carriage away from dock bays) and X second. Step deltas between fake
        start and trigger point back-calculate real dock coordinates.

        Results are written directly into bt_customize.cfg's variable_x_locs /
        variable_dock_y lines (with a timestamped backup taken first), which
        Klipper re-reads on the next restart — no variables.cfg involved.

        Optional:
          TEST=1        — display computed positions only, skip saving.
          AUTO=1,2,4    — comma-separated tool indices to also update, assuming
                           dock bays are spaced evenly 30mm apart from TOOL's
                           measured position. TOOL itself is ignored if listed.
        """
        tool      = gcmd.get_int('TOOL', minval=0)
        test_mode = gcmd.get_int('TEST', 0) != 0

        x_locs = list(self.get_var('x_locs', []))
        num_tools = len(x_locs)
        if tool >= num_tools:
            raise gcmd.error(
                f"BT_CALIBRATE_DOCK: TOOL={tool} out of range (0–{num_tools - 1})")

        auto_tools = self._parse_tool_index_list(gcmd, 'AUTO', num_tools, exclude=tool)

        toolhead = self.printer.lookup_object('toolhead')
        phoming  = self.printer.lookup_object('homing')
        kin      = toolhead.get_kinematics()

        x_endstop = kin.rails[0].get_endstops()[0][0]
        y_endstop = kin.rails[1].get_endstops()[0][0]

        # Use position_max as the fake starting coordinate — it is within the valid
        # axis range so check_move accepts all probe targets, and it is always >=
        # any real dock coordinate so probing moves toward the (min) endstops correctly.
        fake_x = self.x_max                    # 215
        fake_y = self.y_max                    # 218
        fake_z = toolhead.get_position()[2]    # Reuse current Z (was valid before M84)

        # Establish fake coordinate frame and mark all axes as homed. This sets the
        # kinematic limits to [position_min, position_max] for each axis, which is
        # what Klipper requires before accepting move/probe commands.
        self._set_position_homed(toolhead, [fake_x, fake_y, fake_z, 0.0])

        # --- Phase 1: probe Y ---
        # Target y_endstop_pos equals position_min_y, which is within the valid range.
        # Probing Y first moves the carriage away from dock bays so X traversal is safe.
        cur = toolhead.get_position()
        y_target = [cur[0], self.y_endstop_pos, cur[2], cur[3]]
        epos_y = phoming.probing_move(y_endstop, y_target, self.y_homing_speed)
        dock_y = fake_y - epos_y[1] + self.y_endstop_pos

        # No intermediate set_position needed: after Y probe epos_y[1] is always
        # within [position_min_y, position_max_y], so the X probe target passes
        # check_move without any Y correction.

        # --- Phase 2: probe X (physical endstop) ---
        # Carriage is at Y=y_endstop (away from dock bays); X still at physical dock_x.
        # Target x_endstop_pos equals position_min_x, within valid range.
        cur = toolhead.get_position()
        x_target = [self.x_endstop_pos, cur[1], cur[2], cur[3]]
        epos_x = phoming.probing_move(x_endstop, x_target, self.x_homing_speed)
        dock_x = fake_x - epos_x[0] + self.x_endstop_pos

        # After both probes the carriage physically sits at both min endstops. The
        # probe coordinate frame is still offset from reality by fake_* though, so
        # re-establish the frame to match physical reality (carriage = endstop
        # positions). Without this, backing off can exceed the axis limits when the
        # dock is near an endstop (the offset frame reports a position near fake_max).
        cur = toolhead.get_position()
        self._set_position_homed(
            toolhead, [self.x_endstop_pos, self.y_endstop_pos, cur[2], cur[3]])

        # Back off both axes ~8mm from the min endstops before the standard home so
        # G28 doesn't drive the carriage hard into the physical ends (homing_retract_dist
        # may be 0). manual_move operates in the raw kinematic frame.
        backoff = 8.0
        toolhead.manual_move(
            [self.x_endstop_pos + backoff, self.y_endstop_pos + backoff, None, None],
            self.x_homing_speed)
        toolhead.wait_moves()

        # Home properly (Y before X per the G28 override).
        self.gcode.run_script_from_command("G28")

        # Compute TOOL's position, then any AUTO tools by 30mm-per-bay spacing.
        x_locs[tool] = round(dock_x, 2)
        for at in auto_tools:
            x_locs[at] = round(dock_x + 30.0 * (at - tool), 2)

        if test_mode:
            auto_lines = "".join(
                f"\n  x_locs[{at}] = {x_locs[at]:.2f} mm  (AUTO)" for at in auto_tools)
            gcmd.respond_info(
                f"=== BT_CALIBRATE_DOCK TOOL={tool} [TEST — not saved] ===\n"
                f"  dock_x = {dock_x:.2f} mm\n"
                f"  dock_y = {dock_y:.2f} mm  (shared across all tools)"
                f"{auto_lines}\n"
                f"  current x_locs = {x_locs}"
            )
            return

        # Persist to bt_customize.cfg (single source of truth; no variables.cfg).
        x_locs_str = "[" + ", ".join(str(v) for v in x_locs) + "]"
        backup_path = self._write_customize_updates({
            'x_locs': x_locs_str,
            'dock_y': str(round(dock_y, 2)),
        })

        self.set_var('x_locs', x_locs)
        self.set_var('dock_y', round(dock_y, 2))

        auto_lines = "".join(
            f"\n  x_locs[{at}] = {x_locs[at]:.2f} mm  (AUTO)" for at in auto_tools)
        gcmd.respond_info(
            f"=== BT_CALIBRATE_DOCK TOOL={tool} ===\n"
            f"  dock_x  = {dock_x:.2f} mm  (saved to x_locs[{tool}])\n"
            f"  dock_y  = {dock_y:.2f} mm  (saved globally)"
            f"{auto_lines}\n"
            f"  x_locs  = {x_locs}\n"
            f"  written to bt_customize.cfg (backup: {backup_path})"
        )

    def BT_SAVE_TOOL_OFFSET(self, gcmd):
        """
        Saves X/Y (and optionally Z) offset for a physical tool slot.
        Usage: BT_SAVE_TOOL_OFFSET TOOL=1 X=0.640 Y=-0.740
               BT_SAVE_TOOL_OFFSET TOOL=1 X=0.640 Y=-0.740 Z=0.043
        If Z is omitted, the existing Z offset for that tool is preserved.
        """
        tool = gcmd.get_int('TOOL', minval=0)
        x = gcmd.get_float('X')
        y = gcmd.get_float('Y')
        offsets = list(self.get_var('tool_offsets', []))
        while len(offsets) <= tool:
            offsets.append([0.0, 0.0, 0.0])
        existing_z = offsets[tool][2] if len(offsets[tool]) > 2 else 0.0
        z = gcmd.get_float('Z', existing_z)
        offsets[tool] = [round(x, 4), round(y, 4), round(z, 4)]
        self.set_var('tool_offsets', offsets)
        self.gcode.run_script_from_command(
            f'SAVE_VARIABLE VARIABLE=bt_xoffset_{tool} VALUE={round(x, 4)}'
        )
        self.gcode.run_script_from_command(
            f'SAVE_VARIABLE VARIABLE=bt_yoffset_{tool} VALUE={round(y, 4)}'
        )
        self.gcode.run_script_from_command(
            f'SAVE_VARIABLE VARIABLE=bt_zoffset_{tool} VALUE={round(z, 4)}'
        )
        gcmd.respond_info(f'Saved offset for T{tool}: X={x} Y={y} Z={z}')

        # If saving Z for the currently active tool, sync the live gcode Z offset
        # by the same delta. Without this, the baby-step formula in BT_CHANGE_TOOL
        # would see (old homing_origin.z - new tool_offsets Z) and mistake the
        # calibration change for a user baby-step on the next toolchange.
        z_delta = round(z, 4) - existing_z
        if tool == self.get_var('current_tool', -1) and abs(z_delta) > 1e-6:
            self.gcode.run_script_from_command(
                f'SET_GCODE_OFFSET Z_ADJUST={z_delta:.4f} MOVE=0'
            )

    # --- Z PROBE CALIBRATION ---

    def ZPROBE_TOOL_OFFSET(self, gcmd):
        """
        Probes the physical Z position of the calibration switch placed on the bed.
        The toolhead must already be positioned directly over the switch (X/Y) and
        within 2–15 mm above it (Z) before calling this command.

        Performs multiple samples with tolerance checking, averages the results,
        and stores the raw physical Z trigger position in last_zprobe.
        The toolhead is returned to its pre-probe position when complete.

        Optional parameters (override bt_zprobe.cfg defaults):
          SPEED=<mm/s>    LIFT_SPEED=<mm/s>    SAMPLES=<n>
          TOLERANCE=<mm>  RETRIES=<n>
        """
        if self.zprobe_pin is None:
            raise gcmd.error(
                "ZPROBE_TOOL_OFFSET: Z probe not configured. "
                "Ensure bt_zprobe.cfg is included in your printer config.")

        toolhead = self.printer.lookup_object('toolhead')
        phoming = self.printer.lookup_object('homing')

        # Allow per-call parameter overrides
        speed        = gcmd.get_float('SPEED',        self.zprobe_speed,        above=0.)
        lift_speed   = gcmd.get_float('LIFT_SPEED',   self.zprobe_lift_speed,   above=0.)
        samples      = gcmd.get_int(  'SAMPLES',      self.zprobe_samples,      minval=1)
        tolerance    = gcmd.get_float('TOLERANCE',    self.zprobe_tolerance,    minval=0.)
        retries      = gcmd.get_int(  'RETRIES',      self.zprobe_retries,      minval=0)
        max_descend  = gcmd.get_float('MAX_DESCEND',  self.zprobe_max_descend,  above=0.)

        # Save physical start position so we can restore it after the full sequence
        start_pos = list(toolhead.get_position())

        gcmd.respond_info(
            "[BBOX] ZPROBE_TOOL_OFFSET: starting (samples={}, tolerance={:.4f}mm, "
            "speed={:.1f}mm/s)".format(samples, tolerance, speed))

        positions   = []
        retry_count = 0

        while len(positions) < samples:
            # Target Z: clamp to [0, current_z) so we stay in range but descend far enough
            cur_pos  = list(toolhead.get_position())
            target_z = max(0.0, cur_pos[2] - max_descend)
            target   = [cur_pos[0], cur_pos[1], target_z, cur_pos[3]]
            #gcmd.respond_info("  probing from Z={:.4f} toward Z={:.4f}".format(cur_pos[2], target_z))

            try:
                epos = phoming.probing_move(self.zprobe_pin, target, speed)
            except self.printer.command_error as e:
                raise gcmd.error("ZPROBE_TOOL_OFFSET: probe move failed: {}".format(e))

            # epos[2] is the raw physical Z at trigger (no gcode offsets applied)
            phys_z = epos[2]
            positions.append(phys_z)
            gcmd.respond_info("  sample {}/{}: Z={:.4f}mm".format(
                len(positions), samples, phys_z))

            # Check spread tolerance after each new sample
            if len(positions) > 1:
                spread = max(positions) - min(positions)
                if spread > tolerance:
                    retry_count += 1
                    if retry_count > retries:
                        raise gcmd.error(
                            "ZPROBE_TOOL_OFFSET: sample spread {:.4f}mm exceeds tolerance "
                            "{:.4f}mm after {} retr{}.".format(
                                spread, tolerance, retries,
                                "y" if retries == 1 else "ies"))
                    gcmd.respond_info(
                        "  spread {:.4f}mm exceeds tolerance — retry {}/{}".format(
                            spread, retry_count, retries))
                    positions = []

            # Lift back to start Z before the next sample (skip after the final good sample)
            if len(positions) < samples:
                toolhead.manual_move([None, None, start_pos[2]], lift_speed)
                toolhead.wait_moves()

        avg_z = sum(positions) / len(positions)
        gcmd.respond_info(
            "[BBOX] ZPROBE_TOOL_OFFSET: {} samples averaged → physical Z = {:.4f}mm".format(
                samples, avg_z))

        # Store result in _BT_VARIABLES
        self.set_var('last_zprobe', round(avg_z, 4))

        # Restore toolhead to pre-probe physical position
        toolhead.manual_move([start_pos[0], start_pos[1], start_pos[2]], lift_speed)
        toolhead.wait_moves()
        gcmd.respond_info(
            "[BBOX] ZPROBE_TOOL_OFFSET: complete. last_zprobe={:.4f}mm. "
            "Toolhead restored.".format(avg_z))

    def is_printing(self):
        """Checks if Klipper is currently executing a print job."""
        print_stats = self.printer.lookup_object('print_stats', None)
        if print_stats:
            # States include: "printing", "paused", "standby", "error", "complete"
            return print_stats.state == "printing"
        return False

    def execute_move_and_wait(self, script):
        """Runs a G-code script and blocks Python until moves finish."""
        self.gcode.run_script_from_command(script)
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.wait_moves()

    def _handle_print_start(self, eventtime):
        # This method is called when a print starts, as registered in the proxy's __init__.
        # We can use this to reset the printing streak counter so that it only counts consecutive successes during a single print job.
        # We also use this opportunity to scan the G-code file for expected tool changes that we can expose to any user and report that to the console.
        print_stats = self.printer.lookup_object('print_stats')
        if not print_stats:
            self.gcode.respond_info("[BBOX] WARNING: print_stats object not found, unable to track printing streaks or expected tool changes.")
            return
        
        stats = print_stats.get_status(eventtime)
        if not stats:
            self.gcode.respond_info("[BBOX] WARNING: No active print job detected.")
            return
        
        # Check if a file is actually being processed
        # stats['state'] will be 'printing' for a file, but often 'standby' for G28
        if stats.get('state') != 'printing' or not stats.get('filename'):
            # This was likely a G28, a heater change, or a manual move
            return
            
        filename = stats.get('filename')
        if not filename:
            self.gcode.respond_info("[BBOX] WARNING: Active print job has no filename, unable to calculate expected tool changes.")
            return

        if filename:
            # Resolve the full path via virtual_sdcard
            sdcard = self.printer.lookup_object('virtual_sdcard')
            # Typically, Klipper stores files in a 'gcodes' folder defined in config
            # but using virtual_sdcard object is safer than assuming the 'config' folder
            full_path = os.path.join(sdcard.sdcard_dirname, filename)
            self.gcode.respond_info(f"[BBOX] Examining G-code file for expected tool changes:\{full_path}")
            totalChanges = self.get_total_toolchanges(full_path)
            self.set_var('expected_changes', totalChanges)
            self.set_var('success_streak_printing', 0)

    def get_total_toolchanges(self, filepath):
        # OrcaSlicer key we are looking for
        search_key = "; total filament change ="
        # 256KB buffer to clear thumbnails and config dumps
        buffer_size = 256 * 1024

        try:
            if not os.path.exists(filepath):
                self.gcode.respond_info(f"[BBOX] File not found: {filepath}")
                return 0

            with open(filepath, 'rb') as f:
                f.seek(0, os.SEEK_END)
                file_size = f.tell()
                
                seek_pos = max(0, file_size - buffer_size)
                f.seek(seek_pos, os.SEEK_SET)
                
                chunk = f.read(buffer_size).decode('utf-8', errors='ignore')
                
                # Use regex to find the number after the key
                # \s* handles any varying whitespace
                pattern = re.escape(search_key) + r"\s*(\d+)"
                match = re.search(pattern, chunk)
                
                if match:
                    return int(match.group(1))
                    
        except Exception as e:
            self.printer.add_log(f"BBox Toolchanger Error: {str(e)}")
            
        return 0
