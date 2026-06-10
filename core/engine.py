"""
core/engine.py — Event-driven internet alarm state machine.

Architecture
────────────────────────────────────────────────────────────────────────────
  PingWorker  (daemon thread)
      Pings PING_TARGETS every PING_INTERVAL seconds.
      Pushes PING_OK or PING_FAIL onto the event queue.
      Uses threading.Event for clean shutdown without busy-sleep.

  Engine.run()  (blocking — call from a dedicated daemon thread)
      Drains the event queue in a tight loop.
      Feeds ping events into _InternetFSM.
      Computes the effective alarm = FSM state × manual override.
      Writes PLC + Moxa ONLY when the effective alarm changes.
      Logs to the alarm file ONLY on state transitions.
      SYNC events force a re-write without changing the logged state
      (used after a config or override change so hardware catches up).

Internet FSM
────────────────────────────────────────────────────────────────────────────

   ┌─────────────────────────────────────────────────────┐
   │                                                     │
   ▼  PING_FAIL          PING_FAIL                       │ PING_OK (any)
  OK  ──────►  CONFIRMING(1)  ──────►  CONFIRMING(2)    │
              PING_OK │              PING_OK │           │
                      │                     │           │
                      └─────────┬───────────┘           │
                                ▼ PING_FAIL              │
                             FAILED ──────────────────────┘

  PING_OK from any state resets to OK immediately.
  FAILED is entered only after FAIL_THRESHOLD consecutive failures.

Manual override  (stored in config/config.json, key "manual_override")
────────────────────────────────────────────────────────────────────────────
  None   →  alarm follows FSM  (automatic mode)
  True   →  alarm forced ON    (regardless of FSM state)
  False  →  alarm forced OFF   (regardless of FSM state)
"""

from __future__ import annotations

import dataclasses
import enum
import logging
import queue
import socket
import ssl
import threading
from datetime import datetime, timezone
from typing import Any

from core.alarms import AlarmManager
from core.moxa import MoxaClient, MoxaError
from core.plc import FINSClient, FINSError
from config.store import Store

log = logging.getLogger(__name__)

# ── tunables ──────────────────────────────────────────────────────────────────

PING_INTERVAL  : int   = 10             # seconds between ping cycles
PING_TARGETS   : tuple = ('1.1.1.1', '8.8.8.8')
FAIL_THRESHOLD : int   = 3             # consecutive failures to enter FAILED


# ── event vocabulary ──────────────────────────────────────────────────────────

class _Evt(enum.Enum):
    PING_OK  = 'PING_OK'
    PING_FAIL = 'PING_FAIL'
    SYNC     = 'SYNC'    # re-write outputs without alarm log (config/override change)
    STOP     = 'STOP'


@dataclasses.dataclass(slots=True)
class _Msg:
    evt  : _Evt
    data : Any = None


# ── internet FSM ──────────────────────────────────────────────────────────────

class _InetState(enum.Enum):
    OK         = 'OK'
    CONFIRMING = 'CONFIRMING'
    FAILED     = 'FAILED'


class _InternetFSM:
    """
    Three-step confirmation state machine.
    Pure logic — no I/O, no side effects.
    """

    def __init__(self) -> None:
        self.state : _InetState = _InetState.OK
        self.count : int        = 0   # consecutive fail counter

    def feed(self, evt: _Evt) -> bool:
        """
        Advance the FSM with one event.
        Returns True if the state changed.
        """
        prev = self.state

        if evt == _Evt.PING_OK:
            self.state = _InetState.OK
            self.count = 0

        elif evt == _Evt.PING_FAIL:
            self.count += 1
            self.state = (
                _InetState.FAILED if self.count >= FAIL_THRESHOLD
                else _InetState.CONFIRMING
            )

        return self.state != prev


# ── published status snapshot ─────────────────────────────────────────────────

@dataclasses.dataclass(slots=True)
class Status:
    """Immutable view produced by Engine.snapshot() for the API layer."""
    inet_state   : str        = _InetState.OK.value
    fail_count   : int        = 0
    alarm_active : bool       = False
    override     : bool | None = None
    plc_ok       : bool | None = None
    moxa_ok      : bool | None = None
    last_event_ts: str | None  = None


# ── engine ────────────────────────────────────────────────────────────────────

class Engine:
    """
    Event-driven alarm engine.

    Usage
    ─────
    engine = Engine(store)
    t = threading.Thread(target=engine.run, daemon=True)
    t.start()
    ...
    engine.sync()   # after a config or override change
    engine.stop()   # clean shutdown
    """

    def __init__(self, store: Store) -> None:
        self._store    : Store        = store
        self._fsm      : _InternetFSM = _InternetFSM()
        self._alarms   : AlarmManager = AlarmManager()
        self._watchdog                = None   # set via set_watchdog()
        self._moxa    : MoxaClient | None = None   # persistent; recreated if IP changes

        self._q    : queue.SimpleQueue[_Msg] = queue.SimpleQueue()
        self._halt : threading.Event         = threading.Event()
        self._lock : threading.Lock          = threading.Lock()

        # Mutable status — protected by _lock
        self._st = Status()

    # ── public ────────────────────────────────────────────────────────────────

    def run(self) -> None:
        """
        Block and process events.  Call from a dedicated thread.
        Starts the ping worker as a daemon sub-thread.
        Forces an initial hardware sync on startup.
        """
        threading.Thread(
            target=self._ping_worker, name='ping-worker', daemon=True,
        ).start()

        # Sync hardware to initial state (override, clean boot, etc.)
        self._q.put(_Msg(_Evt.SYNC))

        log.info(
            'Engine started  interval=%ds  threshold=%d  targets=%s',
            PING_INTERVAL, FAIL_THRESHOLD, PING_TARGETS,
        )
        self._event_loop()

    def sync(self) -> None:
        """
        Request an immediate output re-write.
        Call after updating config or manual override so hardware
        reflects the change without waiting for the next ping cycle.
        """
        self._q.put(_Msg(_Evt.SYNC))

    def stop(self) -> None:
        """Signal clean shutdown."""
        self._halt.set()
        self._q.put(_Msg(_Evt.STOP))

    def set_watchdog(self, watchdog) -> None:
        """Attach a WatchdogManager; called once before run()."""
        self._watchdog = watchdog

    def snapshot(self) -> dict:
        """Thread-safe copy of the current status for the API."""
        with self._lock:
            return dataclasses.asdict(self._st)

    def get_alarms(self) -> list:
        return self._alarms.get_recent()

    # ── event loop ────────────────────────────────────────────────────────────

    def _event_loop(self) -> None:
        while True:
            msg = self._q.get()         # blocks; no busy-wait
            if msg.evt is _Evt.STOP:
                log.info('Engine stopped')
                return
            try:
                self._handle(msg)
            except Exception:
                log.exception('Unhandled error in engine tick')
            # Notify watchdog regardless of _handle outcome — engine loop is alive
            if self._watchdog:
                self._watchdog.notify()

    def _handle(self, msg: _Msg) -> None:
        # 1. Feed ping events into the FSM and log intermediate confirmations.
        if msg.evt in (_Evt.PING_OK, _Evt.PING_FAIL):
            changed = self._fsm.feed(msg.evt)
            self._log_fsm_progress(changed)

        # 2. Read fresh config (picks up IP / channel / override changes).
        cfg      = self._store.get_all()
        override : bool | None = cfg.get('manual_override')

        # 3. Compute effective alarm output.
        alarm = self._effective_alarm(override)

        # 4. Snapshot previous alarm state, then publish new telemetry.
        with self._lock:
            prev_alarm = self._st.alarm_active
            self._st.inet_state    = self._fsm.state.value
            self._st.fail_count    = self._fsm.count
            self._st.override      = override
            self._st.alarm_active  = alarm
            self._st.last_event_ts = datetime.now(timezone.utc).isoformat()

        # 5. Decide whether a hardware write is needed.
        state_changed = alarm != prev_alarm
        if not state_changed and msg.evt is not _Evt.SYNC:
            return

        # 6. Log alarm transition (never log on a plain SYNC re-write).
        if state_changed:
            self._log_alarm_transition(alarm, override)

        # 7. Write PLC and Moxa — outside the lock so I/O never blocks readers.
        self._apply_outputs(alarm, cfg)

    # ── FSM helpers ───────────────────────────────────────────────────────────

    def _effective_alarm(self, override: bool | None) -> bool:
        if override is True:
            return True
        if override is False:
            return False
        return self._fsm.state is _InetState.FAILED

    def _log_fsm_progress(self, state_changed: bool) -> None:
        """Operational logs for intermediate confirmation steps."""
        state = self._fsm.state
        if state is _InetState.CONFIRMING:
            log.warning(
                'Internet check failed — confirming  (%d / %d)',
                self._fsm.count, FAIL_THRESHOLD,
            )
        elif state_changed and state is _InetState.FAILED:
            log.error(
                'Internet FAILED after %d consecutive failures', self._fsm.count,
            )
        elif state_changed and state is _InetState.OK:
            log.info('Internet recovered')

    def _log_alarm_transition(self, alarm: bool, override: bool | None) -> None:
        """Append one JSONL entry and emit a structured log line."""
        if alarm:
            reason = 'manual_override' if override is True else 'internet_failure'
            self._alarms.log_event('ALARM_ON', True, reason)
        else:
            reason = 'manual_override' if override is False else 'internet_recovered'
            self._alarms.log_event('ALARM_OFF', False, reason)
        log.info('Alarm → %-3s  reason=%s', 'ON' if alarm else 'OFF', reason)

    # ── hardware output writer ────────────────────────────────────────────────

    def _apply_outputs(self, alarm: bool, cfg: dict) -> None:
        """
        Write alarm state to PLC and Moxa.
        Always uses fresh config — IP/channel/bit changes take effect immediately.
        Lock is NOT held during network I/O so the API remains responsive.
        """
        try:
            addr   = cfg.get('plc_alarm_bit', 'D1200')
            client = FINSClient(cfg['plc_ip'])
            if '.' in addr:
                client.write_bit(addr, alarm)
            else:
                client.write_word(addr, 1 if alarm else 0)
            plc_ok = True
        except FINSError as exc:
            log.error('PLC: %s', exc)
            plc_ok = False

        try:
            moxa_ip = cfg['moxa_ip']
            if self._moxa is None or self._moxa._ip != moxa_ip:
                if self._moxa is not None:
                    self._moxa.close()
                self._moxa = MoxaClient(moxa_ip)
            self._moxa.set_output(cfg['moxa_channel'], alarm)
            moxa_ok = True
        except MoxaError as exc:
            log.error('Moxa: %s', exc)
            if self._moxa is not None:
                self._moxa.close()
                self._moxa = None
            moxa_ok = False

        with self._lock:
            self._st.plc_ok  = plc_ok
            self._st.moxa_ok = moxa_ok

    # ── ping worker ───────────────────────────────────────────────────────────

    def _ping_worker(self) -> None:
        """
        Runs in its own daemon thread.
        Pings once per PING_INTERVAL, pushes result onto the queue.
        Uses threading.Event.wait() so PING_INTERVAL can be any value
        without blocking shutdown.
        """
        while not self._halt.is_set():
            reachable = any(_ping(h) for h in PING_TARGETS)
            self._q.put(_Msg(_Evt.PING_OK if reachable else _Evt.PING_FAIL))
            self._halt.wait(timeout=PING_INTERVAL)


# ── module-level ping helper ──────────────────────────────────────────────────

def _ping(host: str) -> bool:
    """TLS handshake on port 443.
    Router can intercept the TCP connection but cannot complete the TLS
    ServerHello without the real server's private key — so this only
    returns True when the host is genuinely reachable over the WAN."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    try:
        with socket.create_connection((host, 443), timeout=3) as raw:
            with ctx.wrap_socket(raw):
                return True
    except OSError:
        return False
