"""
Elite Dangerous Status.json -> GremlinEx state sync plugin.

Reads Elite Dangerous' Status.json and mirrors the landing gear, cargo
scoop, and hardpoint deployment bits into GEX states so you can bind
actions/conditions to them in your profile.

REQUIRED SETUP
--------------
1. Drop this file in the same folder as your GEX profile (.xml).
2. Attach it via the profile's Plugins tab.
3. In the State tab of the profile, create a boolean state with the exact
   name (no spaces, lower case) of every key in STATE_FLAG_MAP and
   STATE_VALUE_RULES below. This plugin will not auto-create them;
   create-from-plugin at runtime is fragile and you'll want to bind to
   these in the UI anyway.

TWO WAYS TO DERIVE A STATE
--------------------------
GEX states are boolean (on/off). This plugin builds that boolean two ways:

- STATE_FLAG_MAP: state ON when a bit is set in Status.json "Flags".
  Use this for the many on/off conditions Elite already exposes as flags
  (gear, scoop, silent running, shields up, overheating, ...).

- STATE_VALUE_RULES: state ON when a numeric field from Status.json meets
  a threshold you define, e.g. "weapons capacitor fully pipped" or
  "fuel reservoir nearly empty". Each rule is a small predicate function
  that receives the parsed Status.json dict and returns a bool.

NOT AVAILABLE IN Status.json (so they cannot be synced):
- Numeric heat level: Elite exports no heat percentage. The only heat
  signal is the "Over Heating (>100%)" flag, included below as
  is_overheating.
- Numeric shield level: only the boolean "Shields Up" flag exists
  (is_shields_up). There is no shield strength percentage.
- "Locked on by target": no such flag. The nearest combat-awareness
  signal is IsInDanger (commented out below) but it is not the same thing.
- Orbital lines / rotational correction: these are HUD/flight settings
  that Elite does not write to Status.json.

DESIGN NOTES
------------
- Singleton via gremlin.singleton_decorator so re-imports do not stack
  multiple timers / signal connections.
- Hooks only the documented profile_hook / profile_unhook signals.
- QTimer is created lazily inside profile_hook, so it lives on the GEX
  runtime thread (which has a Qt event loop) rather than whatever thread
  happened to import the module.
- Every Qt slot has a top-level try/except. An exception escaping a slot
  on some PySide6 builds will take the host down.
- States are re-asserted on every poll, not only when Status.json changes.
  GEX resets states to their default on profile activation, so the plugin
  has to continuously drive them or the default wins. setValue() no-ops
  when unchanged, so this does not spam events.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path

from PySide6 import QtCore

import gremlin.event_handler
from gremlin.singleton_decorator import SingletonDecorator


# ---------------------------------------------------------------------------
# User configuration
# ---------------------------------------------------------------------------
# Point at one of:
#   - the Elite Dangerous saved-games folder containing Status.json
#   - a direct path to Status.json
#   - a direct path to a Journal.*.log file (Status.json from same folder)
ELITE_JOURNAL_LOCATION = (
    r"C:\Users\Remilia\Saved Games\Frontier Developments\Elite Dangerous"
)

POLL_INTERVAL_MS = 250
STATUS_STALE_SECONDS = 10.0

# Map of GEX state name -> Elite Dangerous Status.json "Flags" bitmask.
# State is ON when (Flags & mask) != 0. Each key must exist as a boolean
# state in the profile's State tab. Bit values are from the ED Player
# Journal docs (Status File page).
STATE_FLAG_MAP: dict[str, int] = {
    "is_cargo_scoop_down":   0x00000200,  # bit  9: Cargo Scoop Deployed
    "is_landing_gear_down":  0x00000004,  # bit  2: Landing Gear Down
    "is_hardpoint_deployed": 0x00000040,  # bit  6: Hardpoints Deployed
    "is_silent_running":     0x00000400,  # bit 10: Silent Running
    "is_shields_up":         0x00000008,  # bit  3: Shields Up (boolean only)
    "is_overheating":        0x00100000,  # bit 20: Over Heating (> 100%)
    "is_light_on":           0x00000100,  # bit  8: Lights On
    "is_night_vision_on":    0x10000000,  # bit 28: Night Vision

    # No flag means "locked on / scanned by target". IsInDanger is the
    # closest combat-awareness signal (set when in a danger zone), but it
    # is NOT the same as being targeted. Uncomment if you want it anyway,
    # and create a matching "is_in_danger" state.
    # "is_in_danger":        0x00400000,  # bit 22: IsInDanger
}


# GuiFocus values (which GUI screen is active). Not a bitmask -- it's a
# single integer in Status.json. Used by value rules below.
GUI_FOCUS_NO_FOCUS = 0
GUI_FOCUS_GALAXY_MAP = 6
GUI_FOCUS_SYSTEM_MAP = 7
GUI_FOCUS_ORRERY = 8
GUI_FOCUS_FSS = 9   # Full Spectrum System scanner
GUI_FOCUS_SAA = 10  # Detailed Surface Scanner / SAA
GUI_FOCUS_CODEX = 11


# Map of GEX state name -> predicate(payload) -> bool.
# State is ON when the predicate returns True. Use this for anything that
# isn't a simple Flags bit: numeric thresholds, or non-bitmask integer
# fields like GuiFocus. The predicate receives the full parsed Status.json
# dict; keep it cheap and defensive (a field may be absent, e.g. on-foot
# fields only appear on foot). Each key must exist as a boolean state in
# the profile's State tab.
STATE_VALUE_RULES: dict[str, Callable[[dict], bool]] = {
    # FSS scanner open. GuiFocus is an integer screen id, not a flag bit,
    # so it can't live in STATE_FLAG_MAP.
    "is_fss_mode": lambda p: p.get("GuiFocus") == GUI_FOCUS_FSS,

    # ---- more examples (uncomment + create matching states to use) -------
    # Weapons capacitor fully pipped. Pips are half-pips [sys, eng, wep],
    # 0..8, so 8 == 4 pips.
    # "is_weapons_full_pips": lambda p: (p.get("Pips") or [0, 0, 0])[2] >= 8,

    # Fuel reservoir nearly dry (active reservoir tank, in tons).
    # "is_reservoir_low": lambda p: (p.get("Fuel") or {}).get("FuelReservoir", 1.0) < 0.1,

    # On-foot oxygen below 25% (Oxygen is 0.0..1.0, only present on foot).
    # "is_oxygen_low": lambda p: p.get("Oxygen", 1.0) < 0.25,

    # On-foot health below half.
    # "is_health_low": lambda p: p.get("Health", 1.0) < 0.5,
}


syslog = logging.getLogger("system")


@SingletonDecorator
class EliteDangerousStatusSync(QtCore.QObject):
    """Polls Status.json and writes flag bits into named GEX states."""

    def __init__(self) -> None:
        super().__init__()

        self._timer: QtCore.QTimer | None = None
        self._status_path: Path | None = None
        self._last_signature: tuple[int, int] | None = None
        self._last_error: str | None = None
        self._hooked = False
        self._warned_missing_states: set[str] = set()

        # Cache of the values we want the states to hold, recomputed only
        # when Status.json changes but RE-ASSERTED into the states on every
        # poll. See _assert_desired for why.
        self._desired: dict[str, bool] = {}
        # Force the first assert after each activation so it overrides
        # whatever default GEX initialised the states to.
        self._force_next_assert = False

        try:
            el = gremlin.event_handler.EventListener()
            el.profile_hook.connect(self._profile_hook)
            el.profile_unhook.connect(self._profile_unhook)
            syslog.info("ED status sync: plugin loaded, awaiting profile start")
        except Exception:
            # If even wiring the listener fails, log and keep the plugin
            # alive as a no-op rather than crashing the host.
            syslog.exception("ED status sync: failed to connect to EventListener")

    # ---- logging helpers --------------------------------------------------

    def _log_once(self, message: str) -> None:
        """De-duplicate noisy warnings (e.g. file-missing every 250 ms)."""
        if self._last_error != message:
            self._last_error = message
            syslog.warning(message)

    def _clear_error(self) -> None:
        self._last_error = None

    # ---- path resolution --------------------------------------------------

    def _resolve_status_path(self) -> Path:
        configured = Path(ELITE_JOURNAL_LOCATION).expanduser()
        suffix = configured.suffix.lower()
        if suffix == ".json":
            return configured
        if suffix == ".log":
            return configured.parent / "Status.json"
        return configured / "Status.json"

    # ---- state access -----------------------------------------------------

    def _set_state(self, key: str, value: bool, force: bool = False) -> None:
        """Look up a state by name and write to it. Never raises."""
        try:
            # Import lazily: the UI state_device module is not strictly
            # needed until we're running, and a deferred import keeps
            # plugin load surface small.
            import gremlin.ui.state_device as state_device

            state = state_device.StateData().getState(key)
            if state is None:
                if key not in self._warned_missing_states:
                    self._warned_missing_states.add(key)
                    syslog.warning(
                        f"ED status sync: state '{key}' not found. "
                        "Create it in the profile's State tab."
                    )
                return
            state.setValue(bool(value), force=force)
        except Exception:
            self._log_once(f"ED status sync: failed to set state '{key}'")

    def _all_state_names(self):
        """Every state this plugin manages, across both config maps."""
        return (*STATE_FLAG_MAP.keys(), *STATE_VALUE_RULES.keys())

    def _compute_desired(self, payload: dict) -> dict[str, bool]:
        """Build the full {state_name: bool} map from a Status.json payload."""
        try:
            flags = int(payload.get("Flags", 0))
        except (TypeError, ValueError):
            flags = 0

        desired: dict[str, bool] = {
            name: bool(flags & mask) for name, mask in STATE_FLAG_MAP.items()
        }
        for name, predicate in STATE_VALUE_RULES.items():
            try:
                desired[name] = bool(predicate(payload))
            except Exception:
                # A bad/inapplicable rule (e.g. on-foot field while flying)
                # must not break the others or the poll loop.
                self._log_once(
                    f"ED status sync: value rule '{name}' raised; "
                    "treating as False"
                )
                desired[name] = False
        return desired

    def _assert_desired(self, force: bool = False) -> None:
        """Write the cached desired values into the GEX states.

        Called on EVERY poll, not just when Status.json changes. GremlinEx
        resets states to their configured default when a profile is
        activated, so the plugin must continuously re-assert ownership;
        gating writes on file-change alone lets that default clobber our
        value until the file next happens to change (which, sitting still
        in e.g. FSS, may be never).

        setValue() compares against the state's current value and no-ops
        when unchanged, so steady-state re-asserting does not spam
        state-change events. force=True bypasses that and is used once
        right after activation to guarantee an authoritative initial write.
        """
        for name, value in self._desired.items():
            self._set_state(name, value, force=force)

    def _clear_states(self, force: bool = False) -> None:
        """Set every managed state to False (file missing, stale, stop)."""
        self._desired = {name: False for name in self._all_state_names()}
        self._assert_desired(force=force)

    # ---- profile lifecycle slots -----------------------------------------

    @QtCore.Slot()
    def _profile_hook(self) -> None:
        """Called once on profile start."""
        if self._hooked:
            return
        try:
            self._status_path = self._resolve_status_path()
            self._last_signature = None
            self._clear_error()
            self._warned_missing_states.clear()

            # Build the timer here, on the thread that fires profile_hook.
            # That thread has a Qt event loop; the module-import thread
            # might not.
            if self._timer is None:
                self._timer = QtCore.QTimer()
                self._timer.setInterval(POLL_INTERVAL_MS)
                self._timer.timeout.connect(self._poll_status)

            self._hooked = True
            syslog.info(f"ED status sync: watching [{self._status_path}]")

            # Force the first assert so it wins over the default values GEX
            # has just initialised the states to. The regular timer then
            # keeps re-asserting (unforced) to heal any later reset.
            self._force_next_assert = True
            self._poll_status()
            self._timer.start()
        except Exception:
            syslog.exception("ED status sync: profile_hook failed")

    @QtCore.Slot()
    def _profile_unhook(self) -> None:
        """Called once on profile stop."""
        if not self._hooked:
            return
        try:
            if self._timer is not None and self._timer.isActive():
                self._timer.stop()
            self._last_signature = None
            self._hooked = False
            # Best-effort clear so a re-run doesn't start with stale values.
            self._clear_states(force=True)
            syslog.info("ED status sync: profile stopped")
        except Exception:
            syslog.exception("ED status sync: profile_unhook failed")

    # ---- polling ----------------------------------------------------------

    def _read_status_json(self, path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # Elite rewrites the file in place; partial reads happen.
            # Try again next tick.
            return None
        except OSError as exc:
            self._log_once(f"ED status sync: failed reading [{path}]: {exc}")
            return None

    def _is_stale(self, path: Path) -> bool:
        try:
            return (time.time() - path.stat().st_mtime) > STATUS_STALE_SECONDS
        except OSError:
            return True

    @QtCore.Slot()
    def _poll_status(self) -> None:
        if not self._hooked:
            return
        try:
            path = self._status_path or self._resolve_status_path()

            if not path.exists():
                self._log_once(
                    f"ED status sync: Status.json not found at [{path}]"
                )
                self._clear_states()
                return

            if self._is_stale(path):
                self._log_once(
                    f"ED status sync: Status.json older than "
                    f"{STATUS_STALE_SECONDS:.0f}s; treating as inactive"
                )
                self._clear_states()
                return

            try:
                stat = path.stat()
                signature = (stat.st_mtime_ns, stat.st_size)
            except OSError as exc:
                self._log_once(f"ED status sync: stat failed on [{path}]: {exc}")
                self._clear_states()
                return

            # Re-read/parse only when the file actually changed; this gates
            # the comparatively expensive IO + JSON work, not the writes.
            if signature != self._last_signature:
                payload = self._read_status_json(path)
                if payload is not None:
                    self._clear_error()
                    self._last_signature = signature
                    self._desired = self._compute_desired(payload)
                # If payload is None (Elite mid-write), keep the previous
                # desired values and retry on the next tick.

            # Assert every tick, even when the file is unchanged, so that a
            # GremlinEx default-reset on (re)activation is corrected within
            # one poll interval instead of staying stuck until the file
            # next changes.
            self._assert_desired(force=self._force_next_assert)
            self._force_next_assert = False
        except Exception:
            # Top-level guard: never let an exception escape a Qt slot.
            syslog.exception("ED status sync: poll iteration failed")


# Module-level instance. SingletonDecorator guarantees that even if GEX
# re-imports the module, only one real instance exists.
instance = EliteDangerousStatusSync()