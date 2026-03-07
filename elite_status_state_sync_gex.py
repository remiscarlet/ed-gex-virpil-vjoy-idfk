from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from PySide6 import QtCore

import gremlin.event_handler
import gremlin.ui.state_device


# ---------------------------------------------------------------------------
# User configuration
# ---------------------------------------------------------------------------
# You can point this at:
#   1) the Elite Dangerous folder that contains Journal.*.log and Status.json
#   2) a direct path to Status.json
#   3) a direct path to a Journal.*.log file (the plugin will use that file's
#      parent directory and then read Status.json from there)
ELITE_JOURNAL_LOCATION = (
    r"C:\Users\Remilia\Saved Games\Frontier Developments\Elite Dangerous"
)

# Polling interval for Status.json.
POLL_INTERVAL_MS = 250

# If Status.json is older than this, treat it as stale and clear the states.
STATUS_STALE_SECONDS = 10.0

# State names to sync.
CARGO_STATE_NAME = "is_cargo_scoop_down"
GEAR_STATE_NAME = "is_landing_gear_down"

# Safety net only. For design-time mapping, create these states manually in the
# GremlinEx State tab with the exact same names.
REGISTER_MISSING_STATES = True


# Elite Dangerous Status.json Flags bit masks
FLAG_LANDING_GEAR_DOWN = 0x00000004
FLAG_CARGO_SCOOP_DEPLOYED = 0x00000200


syslog = logging.getLogger("system")


class EliteDangerousStatusStateSync(QtCore.QObject):
    """Sync Elite Dangerous Status.json flags into GremlinEx states."""

    def __init__(self) -> None:
        super().__init__()
        self._listener = gremlin.event_handler.EventListener()
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll_status)

        self._status_path: Path | None = None
        self._last_signature: tuple[int, int] | None = None
        self._last_error: str | None = None
        self._started = False

        self._listener.profile_start.connect(self._on_profile_start)
        self._listener.profile_stop.connect(self._on_profile_stop)
        self._listener.profile_unload.connect(self._on_profile_stop)
        self._listener.profile_unloaded.connect(self._on_profile_stop)
        self._listener.shutdown.connect(self._on_profile_stop)

    def _log_once(self, message: str) -> None:
        if self._last_error != message:
            self._last_error = message
            syslog.warning(message)

    def _clear_error(self) -> None:
        self._last_error = None

    def _resolve_status_path(self) -> Path:
        configured = Path(ELITE_JOURNAL_LOCATION).expanduser()

        if configured.suffix.lower() == ".json":
            return configured

        if configured.suffix.lower() == ".log":
            return configured.parent / "Status.json"

        return configured / "Status.json"

    def _ensure_states(self) -> None:
        sd = gremlin.ui.state_device.StateData()
        wanted = {
            CARGO_STATE_NAME: "Elite Dangerous cargo scoop deployment state",
            GEAR_STATE_NAME: "Elite Dangerous landing gear deployment state",
        }

        for key, description in wanted.items():
            if sd.getState(key) is None:
                if not REGISTER_MISSING_STATES:
                    raise RuntimeError(
                        f"Required GEX state '{key}' does not exist. "
                        "Create it in the State tab or enable REGISTER_MISSING_STATES."
                    )
                sd.register(key, False, description)
                syslog.info(f"ED status sync: registered missing state [{key}]")

    def _set_state(self, key: str, value: bool, force: bool = False) -> None:
        sd = gremlin.ui.state_device.StateData()
        state = sd.getState(key)
        if state is None:
            self._ensure_states()
            state = sd.getState(key)
        if state is None:
            return
        state.setValue(bool(value), force=force)

    def _set_states(self, cargo_down: bool, gear_down: bool, force: bool = False) -> None:
        self._set_state(CARGO_STATE_NAME, cargo_down, force=force)
        self._set_state(GEAR_STATE_NAME, gear_down, force=force)

    @QtCore.Slot()
    def _on_profile_start(self) -> None:
        self._status_path = self._resolve_status_path()
        self._last_signature = None
        self._clear_error()
        self._ensure_states()
        self._started = True
        self._poll_status()
        self._timer.start()
        syslog.info(f"ED status sync: watching [{self._status_path}]")

    @QtCore.Slot()
    def _on_profile_stop(self) -> None:
        if self._timer.isActive():
            self._timer.stop()
        self._last_signature = None
        self._started = False
        # Clear stale values when the profile stops.
        self._set_states(False, False, force=True)

    def _read_status_json(self, path: Path) -> dict | None:
        try:
            raw = path.read_text(encoding="utf-8")
            return json.loads(raw)
        except json.JSONDecodeError:
            # Elite may update the file while we are reading it.
            return None
        except OSError as exc:
            self._log_once(f"ED status sync: failed reading [{path}]: {exc}")
            return None

    def _is_stale(self, path: Path) -> bool:
        try:
            age_seconds = time.time() - path.stat().st_mtime
            return age_seconds > STATUS_STALE_SECONDS
        except OSError:
            return True

    @QtCore.Slot()
    def _poll_status(self) -> None:
        if not self._started:
            return

        path = self._status_path or self._resolve_status_path()
        if not path.exists():
            self._log_once(f"ED status sync: Status.json not found at [{path}]")
            self._set_states(False, False)
            return

        if self._is_stale(path):
            self._log_once(
                f"ED status sync: Status.json is stale (> {STATUS_STALE_SECONDS}s old) at [{path}]"
            )
            self._set_states(False, False)
            return

        try:
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError as exc:
            self._log_once(f"ED status sync: failed stat on [{path}]: {exc}")
            self._set_states(False, False)
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

        cargo_down = bool(flags & FLAG_CARGO_SCOOP_DEPLOYED)
        gear_down = bool(flags & FLAG_LANDING_GEAR_DOWN)
        self._set_states(cargo_down, gear_down)


# GremlinEx imports the script; keeping a module-level instance alive is enough.
_plugin = EliteDangerousStatusStateSync()
