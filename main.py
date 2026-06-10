"""
Edge SCADA — entry point.
Starts the engine and watchdog in daemon threads, then serves the Flask UI.
"""
import logging
import signal
import threading
import time

from auth.users import UserStore
from config.store import Store
from core.audit import AuditLog
from core.engine import Engine
from core.watchdog import WatchdogManager
from api.routes import create_app

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s',
)

# Docker sends SIGTERM on container stop/reboot.  Werkzeug dev server does not
# handle SIGTERM by default, so Docker falls back to SIGKILL after 10 s.
# SIGKILL bypasses SO_LINGER and may leave the Moxa in CLOSE_WAIT.
# Converting SIGTERM → SystemExit lets app.run() return cleanly so daemon
# threads and their sockets are shut down with the RST linger option intact.
def _handle_sigterm(*_):
    raise SystemExit(0)

signal.signal(signal.SIGTERM, _handle_sigterm)

if __name__ == '__main__':
    start_time = time.time()

    store    = Store()
    engine   = Engine(store)
    audit    = AuditLog()
    _        = UserStore()   # initialise users.json (creates admin account if absent)
    watchdog = WatchdogManager(engine)

    engine.set_watchdog(watchdog)

    threading.Thread(target=engine.run,   name='engine',   daemon=True).start()
    threading.Thread(target=watchdog.run, name='watchdog', daemon=True).start()

    app = create_app(store, engine, audit=audit, watchdog=watchdog, start_time=start_time)
    app.run(host='0.0.0.0', port=8080, threaded=True, use_reloader=False)
