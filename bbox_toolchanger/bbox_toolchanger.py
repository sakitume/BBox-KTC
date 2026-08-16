import os
import sys
import logging
import importlib

EXTRAS_DIR = os.path.dirname(__file__)
if EXTRAS_DIR not in sys.path:
    sys.path.append(EXTRAS_DIR)

import bbox_toolchanger_impl

class ToolMacroProxy:
    """Fake gcode_macro object Mainsail reads for per-tool color and active state.

    Registered as 'gcode_macro T0', 'gcode_macro T1', etc. so Mainsail's
    extruder panel finds it when it looks up gcode_macro {tool_name}.
    """
    def __init__(self, proxy, tool_index):
        self._proxy = proxy
        self._tool_index = tool_index

    def get_status(self, eventtime):
        try:
            status = self._proxy.impl.get_status(eventtime)
        except Exception:
            return {'active': False, 'color': '000000', 'colour': '000000'}

        colors = status.get('gate_color', [])
        current_tool = status.get('tool', -1)
        color = colors[self._tool_index] if self._tool_index < len(colors) else '000000'
        active = (current_tool == self._tool_index)
        return {'active': active, 'color': color, 'colour': color}


class BBoxToolChangerProxy:
    def __init__(self, config):
        self.config = config
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.impl = bbox_toolchanger_impl.ToolLogic(self.printer, config)
        
        # Register for print start events to reset the printing streak counter. This is done in the proxy since it's a cross-cutting concern that involves both the logic and the printer state.
        self.printer.register_event_handler("idle_timeout:printing", self.impl._handle_print_start)

        # Re-run sync_and_init_arrays once Klipper signals all objects are ready.
        # The first attempt in ToolLogic.__init__ silently fails on cold start
        # because save_variables is not yet initialised at extension load time.
        self.printer.register_event_handler("klippy:ready", self._handle_klippy_ready)

        # Register commands
        self.gcode.register_command("BT_TEST_TOOL", self.cmd_TEST_TOOL, 
                                    desc="Tests load/unloading of the specified tool (TOOL) for the specified number of times (COUNT)")
        self.gcode.register_command("BT_TEST_CYCLE", self.cmd_TEST_CYCLE, 
                                    desc="Cycles through all tools COUNT number of times. If SQUARE is provided performs a trace square at the specified speed after each tool change.")
        self.gcode.register_command("BT_CHANGE_TOOL", self.cmd_CHANGE_TOOL, 
                                    desc="Change to tool specified by parameter TOOL. If TOOL is -1 then unload current tool if any")
        self.gcode.register_command("BT_LOAD_TOOL", self.cmd_LOAD_TOOL, 
                                    desc="Load the tool specified by paramer TOOL. Toolhead must be empty or this will fail")
        self.gcode.register_command("BT_UNLOAD_TOOL", self.cmd_UNLOAD_TOOL, 
                                    desc="Unload the current tool. Toolhead must have a tool inserted or this will fail")
        self.gcode.register_command("BT_RELOAD", self.cmd_RELOAD, 
                                    desc="Hot-reload logic from bbox_toolchanger_impl.py")
        self.gcode.register_command("BT_FORCE_TOOL_STATE", self.cmd_FORCE_TOOL_STATE, 
                                    desc="Manually set toolchanger state")
        self.gcode.register_command("BT_STATS", self.cmd_BT_STATS,
                                    desc="Display toolchanger stats")
        self.gcode.register_command("BT_SAVE_TOOL_OFFSET", self.cmd_BT_SAVE_TOOL_OFFSET,
                                    desc="Save X/Y offset for a physical tool. Usage: BT_SAVE_TOOL_OFFSET TOOL=1 X=0.640 Y=-0.740")
        self.gcode.register_command("BT_CALIBRATE_DOCK", self.cmd_BT_CALIBRATE_DOCK,
                                    desc="Capture dock bay position. Run after M84 + manual carriage positioning. Usage: BT_CALIBRATE_DOCK TOOL=N [TEST=1] [AUTO=1,2,4]")
        self.gcode.register_command("BT_FLUSH_STATS", self.cmd_BT_FLUSH_STATS,
                                    desc="Flush deferred toolchange stats to disk")
        self.gcode.register_command("BT_CHECK_CONFIG", self.cmd_BT_CHECK_CONFIG,
                                    desc="Report discovered extruder heaters, auto-managed bt_* heater fans, and any config problems")
        self.gcode.register_command("ZPROBE_TOOL_OFFSET", self.cmd_ZPROBE_TOOL_OFFSET,
                                    desc="Probe Z position of calibration switch on bed. Stores result in last_zprobe. Requires bt_zprobe.cfg.")

        self.gcode.register_command("BT_SET_GATE_MAP", self.cmd_BT_SET_GATE_MAP,
                                    desc="BBox: update gate colors/material/name/temp")
        self.gcode.register_command("BT_SET_TOOL_MAP", self.cmd_BT_SET_TOOL_MAP,
                                    desc="BBox: update virtual-to-physical tool map")

        # Auto-register T0…T(N-1) based on x_locs length so adding a bay automatically
        # creates the corresponding T command after a restart — no bt_tools.cfg needed.
        var_macro = self.printer.lookup_object('gcode_macro _BT_VARIABLES')
        n_tools = len(var_macro.variables.get('x_locs', []))
        self._register_tool_commands(n_tools)

        # Register fake gcode_macro T0…TN objects so Mainsail's extruder panel
        # can read per-tool color and active state without real gcode_macro defs.
        self._tool_macro_proxies = []
        for i in range(n_tools):
            macro_proxy = ToolMacroProxy(self, i)
            self._tool_macro_proxies.append(macro_proxy)
            try:
                self.printer.add_object(f'gcode_macro T{i}', macro_proxy)
            except Exception as e:
                logging.warning(f"[BBox] Could not register gcode_macro T{i}: {e}")

    def _register_tool_commands(self, n_tools):
        for i in range(n_tools):
            def make_handler(tool_idx):
                def handler(gcmd):
                    self.gcode.run_script_from_command(f'BT_CHANGE_TOOL TOOL={tool_idx}')
                return handler
            self.gcode.register_command(
                f'T{i}',
                make_handler(i),
                desc=f'Select tool {i}'
            )

    def _handle_klippy_ready(self):
        """Called by Klipper once all objects (incl. save_variables) are ready.
        Re-runs sync_and_init_arrays so saved colors/maps are loaded from disk.
        """
        try:
            self.impl.sync_and_init_arrays()
        except Exception as e:
            logging.exception("BBox sync_and_init_arrays on klippy:ready failed")
            self.gcode.respond_info(f"[BBOX] WARNING: failed to load saved colors/map: {e}")

    def cmd_BT_STATS(self, gcmd):
        self.impl.BT_STATS(gcmd)

    def cmd_BT_SAVE_TOOL_OFFSET(self, gcmd):
        self.impl.BT_SAVE_TOOL_OFFSET(gcmd)

    def cmd_BT_CALIBRATE_DOCK(self, gcmd):
        try:
            self.impl.BT_CALIBRATE_DOCK(gcmd)
        except Exception as e:
            self.log_exception(e)

    def cmd_BT_FLUSH_STATS(self, gcmd):
        self.impl.BT_FLUSH_STATS(gcmd)

    def cmd_BT_CHECK_CONFIG(self, gcmd):
        try:
            self.impl.BT_CHECK_CONFIG(gcmd)
        except Exception as e:
            self.log_exception(e)

    def cmd_ZPROBE_TOOL_OFFSET(self, gcmd):
        try:
            self.impl.ZPROBE_TOOL_OFFSET(gcmd)
        except Exception as e:
            self.log_exception(e)

    def cmd_FORCE_TOOL_STATE(self, gcmd):
        self.impl.BT_FORCE_TOOL_STATE(gcmd)                                    

    def cmd_TEST_TOOL(self, gcmd):
        try:
            self.impl.BT_TEST_TOOL(gcmd)
        except Exception as e:
            self.log_exception(e)

    def cmd_TEST_CYCLE(self, gcmd):
        try:
            self.impl.BT_TEST_CYCLE(gcmd)
        except Exception as e:
            self.log_exception(e)

    def cmd_CHANGE_TOOL(self, gcmd):
        try:
            self.impl.BT_CHANGE_TOOL(gcmd)
        except Exception as e:
            self.log_exception(e)

    def cmd_LOAD_TOOL(self, gcmd):
        try:
            self.impl.BT_LOAD_TOOL(gcmd)
        except Exception as e:
            self.log_exception(e)

    def cmd_UNLOAD_TOOL(self, gcmd):
        try:
            self.impl.BT_UNLOAD_TOOL(gcmd)
        except Exception as e:
            self.log_exception(e)

    def cmd_RELOAD(self, gcmd):
        global bbox_toolchanger_impl
        try:
            self.impl.cleanup()
            importlib.reload(bbox_toolchanger_impl)
            self.impl = bbox_toolchanger_impl.ToolLogic(self.printer, self.config)
            gcmd.respond_info("✓ BBox toolchanger logic reloaded")
        except Exception as e:
            logging.exception("BBox toolchanger reload failed")
            gcmd.respond_info(f"CRITICAL: Reload failed! {str(e)}")
        
    def get_status(self, eventtime):
        return self.impl.get_status(eventtime)

    def cmd_BT_SET_GATE_MAP(self, gcmd):
        self.impl.BT_SET_GATE_MAP(gcmd)

    def cmd_BT_SET_TOOL_MAP(self, gcmd):
        self.impl.BT_SET_TOOL_MAP(gcmd)

    def log_exception(self, e):
        errStr = f"!! Toolchanger Error: {str(e)}"
        logging.exception(errStr)
        self.gcode.respond_info(errStr)

def load_config(config):
    return BBoxToolChangerProxy(config)