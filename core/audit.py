"""
core/audit.py — Append-only audit log for config changes and auth events.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_AUDIT_FILE: Path = Path('/app/data/audit.jsonl')


class AuditLog:
    def __init__(self) -> None:
        _AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not _AUDIT_FILE.exists():
            _AUDIT_FILE.touch()

    def log(
        self,
        username:  str,
        action:    str,
        old_value: Any = None,
        new_value: Any = None,
    ) -> None:
        entry = {
            'ts':        datetime.now(timezone.utc).isoformat(),
            'username':  username,
            'action':    action,
            'old_value': old_value,
            'new_value': new_value,
        }
        try:
            with _AUDIT_FILE.open('a') as f:
                f.write(json.dumps(entry) + '\n')
        except OSError as exc:
            log.error('Audit write failed: %s', exc)
