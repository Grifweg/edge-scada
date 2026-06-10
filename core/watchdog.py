"""
core/watchdog.py — Software watchdog that runs independently from the engine.

The engine calls notify() on every event-loop cycle.
If notify() is not called within TIMEOUT seconds the watchdog logs
WATCHDOG_TIMEOUT, creates an alarm event, and marks the engine as failed.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.engine import Engine

log = logging.getLogger(__name__)

_WATCHDOG_FILE  : Path = Path('/app/data/watchdog.json')
UPDATE_INTERVAL : int  = 5    # seconds between watchdog ticks
TIMEOUT         : int  = 30   # seconds of silence before declaring engine dead


class WatchdogManager:
    """
    Runs in its own daemon thread, completely independent of the engine.
    The engine must call :meth:`notify` on each event-loop iteration.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine    = engine
        self._last_seen = datetime.now(timezone.utc)
        self._ok        = True
        self._lock      = threading.Lock()
        self._halt      = threading.Event()
        self._start     = datetime.now(timezone.utc)
        _WATCHDOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    # ── Public ────────────────────────────────────────────────────────────────

    def notify(self) -> None:
        """Called by the engine on every event-loop cycle."""
        with self._lock:
            self._last_seen = datetime.now(timezone.utc)

    @property
    def is_ok(self) -> bool:
        with self._lock:
            return self._ok

    @property
    def uptime_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self._start).total_seconds()

    def run(self) -> None:
        """Blocking entry-point — call from a daemon thread."""
        log.info('Watchdog started  interval=%ds  timeout=%ds', UPDATE_INTERVAL, TIMEOUT)
        while not self._halt.is_set():
            self._tick()
            self._halt.wait(UPDATE_INTERVAL)

    def stop(self) -> None:
        self._halt.set()

    # ── Internals ─────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        now = datetime.now(timezone.utc)

        with self._lock:
            elapsed  = (now - self._last_seen).total_seconds()
            prev_ok  = self._ok
            self._ok = elapsed < TIMEOUT

        if not self._ok and prev_ok:
            log.error('WATCHDOG_TIMEOUT: engine has not cycled for %.0fs', elapsed)
            try:
                self._engine._alarms.log_event(
                    'WATCHDOG_TIMEOUT', True,
                    f'engine frozen {elapsed:.0f}s'
                )
            except Exception:
                pass

        snap = {}
        try:
            snap = self._engine.snapshot()
        except Exception:
            pass

        data = {
            'last_seen':      now.isoformat(),
            'engine_running': self.is_ok,
            'internet_state': snap.get('inet_state') == 'OK',
            'alarm_state':    snap.get('alarm_active', False),
            'uptime_seconds': (now - self._start).total_seconds(),
        }
        try:
            with _WATCHDOG_FILE.open('w') as f:
                json.dump(data, f, indent=2)
        except OSError as exc:
            log.error('Watchdog write failed: %s', exc)
