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
   name (no spaces, lower case) of every key in STATE_FLAG_MAP below.
   This plugin will not auto-create them; create-from-plugin at runtime
   is fragile and you'll want to bind to these in the UI anyway.

To expose more Status.json flags, just add entries to STATE_FLAG_MAP.

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
"""

from __future__ import annotations

import json
import logging
import time
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

# Map of GEX state name -> Elite Dangerous Status.json Flags bitmask.
# Each key must exist as a boolean state in the profile's State tab.
# Add more entries to expose additional flags; the rest of the plugin
# adapts automatically. Bit values are from the ED Player Journal docs.
STATE_FLAG_MAP: dict[str, int] = {
    "is_cargo_scoop_down":   0x00000200,  # Cargo Scoop Deployed
    "is_landing_gear_down":  0x00000004,  # Landing Gear Down
    "is_hardpoint_deployed": 0x00000040,  # Hardpoints Deployed
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

    def _apply_flags(self, flags: int, force: bool = False) -> None:
        """Write each configured state from its bit in the flags int."""
        for state_name, mask in STATE_FLAG_MAP.items():
            self._set_state(state_name, bool(flags & mask), force=force)

    def _clear_states(self, force: bool = False) -> None:
        """Set every configured state to False (file missing, stale, stop)."""
        for state_name in STATE_FLAG_MAP:
            self._set_state(state_name, False, force=force)

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

            # First poll synchronously so states are correct before the
            # user's first input frame.
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

            if signature == self._last_signature:
                return

            payload = self._read_status_json(path)
            if payload is None:
                return

            self._clear_error()
            self._last_signature = signature

            try:
                flags = int(payload.get("Flags", 0))
            except (TypeError, ValueError):
                flags = 0

            self._apply_flags(flags)
        except Exception:
            # Top-level guard: never let an exception escape a Qt slot.
            syslog.exception("ED status sync: poll iteration failed")


# Module-level instance. SingletonDecorator guarantees that even if GEX
# re-imports the module, only one real instance exists.
instance = EliteDangerousStatusSync()