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
- Reload-safe single-instance enforcement via a sentinel attribute on
  EventListener (which is the only true app-wide singleton in GEX).
  @SingletonDecorator alone is NOT enough -- it gets re-instantiated on
  every module reload, which GEX does on each profile activation, so the
  decorator version accumulated ~20 zombie instances over a normal play
  session. See the bottom of the file for the actual guard.
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
- Status.json staleness threshold is generous (5 min) because Elite does
  not heartbeat the file. During idle gameplay, real updates can be 30+
  seconds apart; a tight threshold causes false-positive flicker.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path

from PySide6 import QtCore

import gremlin.event_handler


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
# Treat Status.json as "Elite is no longer running" only after this many
# seconds without an mtime bump. Elite does not heartbeat Status.json --
# during idle play (e.g. sitting still in FSS), real updates can be 30+
# seconds apart. A small window here causes false-positive flicker during
# normal idle gameplay. 5 minutes is comfortably past Elite's actual write
# cadence while still catching "Elite quit, file left behind" within a
# reasonable window.
STATUS_STALE_SECONDS = 300.0

# Diagnostic: when True, every change to a managed state is logged at INFO
# with the triggering Flags integer and the payload's timestamp. Use this
# if you see oscillation to identify whether the source is the file
# content itself or something downstream. Leave False in normal use.
DEBUG_LOG_STATE_CHANGES = False

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
    "is_analysis_mode":      0x08000000,  # bit 27: Hud in Analysis mode

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
        # Timestamp of the last poll where we either successfully refreshed
        # from Status.json or confirmed the cached signature is still
        # current. Used to decide when to give up and clear states. None
        # means "never had a good read yet" and counts as stale.
        self._last_good_read_time: float | None = None

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
            self._last_good_read_time = None
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

    def _try_refresh_desired(self, path: Path) -> bool:
        """Attempt to update self._desired from Status.json.

        Returns True if we either parsed a fresh payload OR confirmed the
        cached signature is still current and the file is not mtime-stale.
        Returns False on any transient or persistent failure (missing,
        locked, mid-write, stale). Callers should NOT clear state on a
        False return -- _poll_status handles that based on how long we've
        been without a successful read.
        """
        if not path.exists():
            self._log_once(
                f"ED status sync: Status.json not found at [{path}]"
            )
            return False

        if self._is_stale(path):
            self._log_once(
                f"ED status sync: Status.json older than "
                f"{STATUS_STALE_SECONDS:.0f}s; treating as inactive"
            )
            return False

        try:
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError as exc:
            self._log_once(f"ED status sync: stat failed on [{path}]: {exc}")
            return False

        if signature == self._last_signature:
            # File is live and unchanged since our last successful read,
            # so cached _desired is still current. This counts as a good
            # read for staleness purposes.
            return True

        payload = self._read_status_json(path)
        if payload is None:
            # Elite likely mid-write; retry on the next tick. NOT a good
            # read -- but also not yet a reason to clear state, since
            # we'll almost certainly succeed on the next poll.
            return False

        # Reject structurally-valid-but-incomplete writes. Elite has been
        # observed to briefly produce a JSON document that parses cleanly
        # but is missing "Flags" (a stub written before fields are filled
        # in). Without this guard, payload.get("Flags", 0) returns 0 and
        # EVERY flag-derived state goes False for that one tick -- which
        # looks exactly like the oscillation you'd see at the 4Hz poll.
        if not isinstance(payload, dict) or "Flags" not in payload:
            self._log_once(
                "ED status sync: Status.json parsed but missing 'Flags'; "
                "treating as a partial write and holding last values"
            )
            return False

        self._clear_error()
        self._last_signature = signature
        new_desired = self._compute_desired(payload)

        if DEBUG_LOG_STATE_CHANGES:
            for name, value in new_desired.items():
                if self._desired.get(name) != value:
                    syslog.info(
                        f"ED state change: {name} {self._desired.get(name)!r}"
                        f" -> {value!r}  (Flags=0x{int(payload.get('Flags', 0)):08X}"
                        f", ts={payload.get('timestamp')!r})"
                    )

        self._desired = new_desired
        return True

    @QtCore.Slot()
    def _poll_status(self) -> None:
        if not self._hooked:
            return
        try:
            path = self._status_path or self._resolve_status_path()
            now = time.time()

            # Attempt to refresh from the file. Transient failures (file
            # briefly missing during Elite's rewrite, stat() racing the
            # write, partial JSON) deliberately do NOT clobber state --
            # they would otherwise cause visible flicker at the 4Hz poll
            # rate, because Elite is writing Status.json constantly.
            if self._try_refresh_desired(path):
                self._last_good_read_time = now

            # Only clear if we've gone a full stale-window without any
            # successful read. That covers Elite not running, a wrong
            # path, or Elite quitting while the profile is active.
            if (
                self._last_good_read_time is None
                or now - self._last_good_read_time > STATUS_STALE_SECONDS
            ):
                cleared = {
                    name: False for name in self._all_state_names()
                }
                if DEBUG_LOG_STATE_CHANGES:
                    for name, value in cleared.items():
                        if self._desired.get(name) != value:
                            syslog.info(
                                f"ED state change: {name} "
                                f"{self._desired.get(name)!r} -> {value!r}  "
                                "(reason: stale clear)"
                            )
                self._desired = cleared

            self._assert_desired(force=self._force_next_assert)
            self._force_next_assert = False
        except Exception:
            # Top-level guard: never let an exception escape a Qt slot.
            syslog.exception("ED status sync: poll iteration failed")


# ---------------------------------------------------------------------------
# Reload-safe registration.
#
# GEX re-executes the plugin module's top-level code on every profile
# activation (and on some other UI events too). @SingletonDecorator only
# guards against multiple instantiations *within a single module load*;
# it does NOT protect against module reloads, because each reload creates
# a fresh decorator instance wrapping a fresh class.
#
# Without this guard, every activation leaves behind a previous instance
# still subscribed to EventListener signals, still running its QTimer,
# still writing to the same GEX states. We have direct log evidence of
# ~20 instances accumulating. Each one independently decides when the
# file is stale, each one writes False at different moments, and the
# states visibly flicker.
#
# The fix: store a sentinel on the EventListener, which IS a real
# application-wide singleton. First load wins. Re-imports are no-ops.
# Cost: editing this file while GEX is running has no effect until you
# fully restart GEX. That is the correct tradeoff -- multiple live copies
# of the plugin are strictly worse than one slightly stale copy.
# ---------------------------------------------------------------------------
_REGISTRY_ATTR = "_ed_status_sync_instance"

_event_listener = gremlin.event_handler.EventListener()
if getattr(_event_listener, _REGISTRY_ATTR, None) is not None:
    syslog.info(
        "ED status sync: already loaded in this GEX session; "
        "skipping duplicate registration. Restart GEX to pick up plugin edits."
    )
else:
    instance = EliteDangerousStatusSync()
    setattr(_event_listener, _REGISTRY_ATTR, instance)
    syslog.info("ED status sync: registered (first load)")