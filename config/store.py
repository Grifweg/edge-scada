"""
config/store.py — Persistent, thread-safe configuration store.

Reads and writes config.json in the same directory as this file.
All public methods are safe to call from multiple threads simultaneously.

Schema
──────
  plc_ip          str        Omron CJ2M IP address
  moxa_ip         str        Moxa ioLogik IP address
  moxa_channel    int 0–7    DO channel to energise on alarm
  manual_override None|bool  None=auto  True=force-ON  False=force-OFF
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

# config.json lives in the same directory as this module
_CONFIG_FILE: Path = Path(__file__).parent / 'config.json'

_DEFAULTS: dict[str, Any] = {
    'plc_ip':          '192.168.1.10',
    'plc_alarm_bit':   'D1200',
    'moxa_ip':         '192.168.1.20',
    'moxa_channel':    0,
    'manual_override': None,
}


class Store:
    """
    Single source of truth for runtime configuration.

    Changes made through :meth:`update` are persisted to disk immediately
    so they survive a container restart.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data = self._load()

    # ── Public ────────────────────────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """Return one config value by key."""
        with self._lock:
            return self._data.get(key, default)

    def get_all(self) -> dict[str, Any]:
        """Return a shallow copy of the entire config dict."""
        with self._lock:
            return dict(self._data)

    def update(self, changes: dict[str, Any]) -> None:
        """Merge *changes* into the current config and persist to disk."""
        with self._lock:
            self._data.update(changes)
            self._persist(self._data)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _load(self) -> dict[str, Any]:
        if _CONFIG_FILE.exists():
            try:
                with _CONFIG_FILE.open() as fh:
                    stored = json.load(fh)
                # Merge: stored values override defaults so unknown keys survive
                return {**_DEFAULTS, **stored}
            except (json.JSONDecodeError, OSError):
                pass
        # First boot — write defaults so the file exists for inspection
        cfg = dict(_DEFAULTS)
        self._persist(cfg)
        return cfg

    @staticmethod
    def _persist(data: dict[str, Any]) -> None:
        try:
            with _CONFIG_FILE.open('w') as fh:
                json.dump(data, fh, indent=2)
        except OSError:
            pass  # read-only FS or permission error — non-fatal
