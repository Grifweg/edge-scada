"""
Event-based alarm logger.
Writes JSONL entries only on state transitions — no polling noise.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_ALARM_FILE = Path('/app/data/alarms.jsonl')
MAX_DISPLAY = 20


class AlarmManager:
    def __init__(self):
        _ALARM_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not _ALARM_FILE.exists():
            _ALARM_FILE.touch()

    def log_event(self, event: str, state: bool, detail: str = '') -> None:
        entry = {
            'ts':     datetime.now(timezone.utc).isoformat(),
            'event':  event,
            'state':  state,
            'detail': detail,
        }
        try:
            with open(_ALARM_FILE, 'a') as f:
                f.write(json.dumps(entry) + '\n')
            log.info('alarm: %s', entry)
        except OSError as exc:
            log.error('alarm write failed: %s', exc)

    def get_recent(self, n: int = MAX_DISPLAY) -> list:
        try:
            with open(_ALARM_FILE) as f:
                lines = f.readlines()
            result = []
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    result.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
                if len(result) >= n:
                    break
            return result
        except OSError as exc:
            log.error('alarm read failed: %s', exc)
            return []
