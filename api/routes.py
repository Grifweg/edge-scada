"""
Flask REST API + HTML frontend.
All business logic lives in the engine; routes are a thin presentation layer.
"""
from __future__ import annotations

import os
import re
import secrets
import time
from functools import wraps
from pathlib import Path

from flask import (Flask, abort, flash, jsonify, redirect,
                   render_template, request, session, url_for)
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf import CSRFProtect

from auth.manager import User, login_manager
from auth.users import UserStore
from config.store import Store
from core.audit import AuditLog
from core.engine import Engine

# ── Input validation ──────────────────────────────────────────────────────────

_IP_RE = re.compile(r'^(\d{1,3}\.){3}\d{1,3}$')


def _valid_ip(s: str) -> bool:
    if not isinstance(s, str) or not _IP_RE.match(s.strip()):
        return False
    return all(0 <= int(p) <= 255 for p in s.split('.'))


# ── Role decorators ───────────────────────────────────────────────────────────

def engineer_required(f):
    @wraps(f)
    @login_required
    def _inner(*args, **kwargs):
        if not current_user.is_engineer:
            if request.path.startswith('/api/'):
                return jsonify({'error': 'forbidden'}), 403
            abort(403)
        return f(*args, **kwargs)
    return _inner


def admin_required(f):
    @wraps(f)
    @login_required
    def _inner(*args, **kwargs):
        if not current_user.is_admin:
            if request.path.startswith('/api/'):
                return jsonify({'error': 'forbidden'}), 403
            abort(403)
        return f(*args, **kwargs)
    return _inner


# ── Secret key (persistent across restarts) ───────────────────────────────────

def _get_secret_key() -> str:
    env_key = os.environ.get('SECRET_KEY')
    if env_key:
        return env_key
    sk_file = Path('/app/data/.secret_key')
    try:
        sk_file.parent.mkdir(parents=True, exist_ok=True)
        if sk_file.exists():
            return sk_file.read_text().strip()
        key = secrets.token_hex(32)
        sk_file.write_text(key)
        return key
    except OSError:
        return secrets.token_hex(32)


# ── App factory ───────────────────────────────────────────────────────────────

def create_app(
    store:      Store,
    engine:     Engine,
    *,
    audit:      AuditLog | None = None,
    watchdog=   None,
    start_time: float | None    = None,
) -> Flask:
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), '..', 'web', 'templates'),
        static_folder=os.path.join(os.path.dirname(__file__), '..', 'web', 'static'),
    )

    app.config['SECRET_KEY']               = _get_secret_key()
    app.config['SESSION_COOKIE_HTTPONLY']  = True
    app.config['SESSION_COOKIE_SAMESITE']  = 'Lax'
    app.config['SESSION_COOKIE_SECURE']    = os.environ.get('HTTPS_ONLY', '').lower() == '1'
    app.config['PERMANENT_SESSION_LIFETIME'] = int(os.environ.get('SESSION_TIMEOUT', '3600'))
    app.config['WTF_CSRF_HEADERS']         = ['X-CSRFToken']
    app.config['WTF_CSRF_SSL_STRICT']      = False  # allow HTTP in development

    csrf  = CSRFProtect(app)
    login_manager.init_app(app)
    login_manager.login_view = 'login_page'

    _audit      = audit or AuditLog()
    _users      = UserStore()
    _start      = start_time or time.time()
    _timeout    = app.config['PERMANENT_SESSION_LIFETIME']

    # ── Session inactivity ────────────────────────────────────────────────────

    @app.before_request
    def _check_inactivity():
        if request.endpoint == 'static':
            return
        if current_user.is_authenticated:
            if time.time() - session.get('_last_active', 0) > _timeout:
                _audit.log(current_user.username, 'SESSION_EXPIRED')
                logout_user()
                session.clear()
                return redirect(url_for('login_page'))
            session['_last_active'] = time.time()

    @app.before_request
    def _check_password_change():
        exempt = frozenset({'login_page', 'change_password_page', 'logout_page', 'static'})
        if (request.endpoint
                and request.endpoint not in exempt
                and current_user.is_authenticated
                and current_user.must_change_password):
            return redirect(url_for('change_password_page'))

    # ── Auth routes ───────────────────────────────────────────────────────────

    @app.route('/login', methods=['GET', 'POST'])
    def login_page():
        if current_user.is_authenticated:
            return redirect(url_for('index'))
        error = None
        if request.method == 'POST':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            if username and _users.verify_password(username, password):
                record = _users.get_user(username)
                user   = User(username, record)
                login_user(user, remember=request.form.get('remember') == 'on')
                session['_last_active'] = time.time()
                _audit.log(username, 'LOGIN')
                next_pg = request.args.get('next', '')
                if next_pg.startswith('/') and not next_pg.startswith('//'):
                    return redirect(next_pg)
                return redirect(url_for('index'))
            error = 'Invalid username or password.'
            _audit.log(username or '?', 'LOGIN_FAILED')
        return render_template('login.html', error=error)

    @app.route('/logout')
    @login_required
    def logout_page():
        _audit.log(current_user.username, 'LOGOUT')
        logout_user()
        session.clear()
        return redirect(url_for('login_page'))

    @app.route('/change-password', methods=['GET', 'POST'])
    @login_required
    def change_password_page():
        error = None
        if request.method == 'POST':
            new_pw  = request.form.get('new_password', '')
            confirm = request.form.get('confirm_password', '')
            if len(new_pw) < 8:
                error = 'Password must be at least 8 characters.'
            elif new_pw != confirm:
                error = 'Passwords do not match.'
            else:
                _users.set_password(current_user.username, new_pw)
                record = _users.get_user(current_user.username)
                login_user(User(current_user.username, record))
                _audit.log(current_user.username, 'PASSWORD_CHANGED')
                return redirect(url_for('index'))
        return render_template('change_password.html', error=error)

    @app.route('/users')
    @admin_required
    def users_page():
        return render_template('users.html', users=_users.list_users())

    @app.route('/users/create', methods=['POST'])
    @admin_required
    def create_user_route():
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        role     = request.form.get('role', 'operator')
        if not username or not password:
            flash('Username and password are required.', 'error')
            return redirect(url_for('users_page'))
        if role not in ('administrator', 'engineer', 'operator'):
            flash('Invalid role.', 'error')
            return redirect(url_for('users_page'))
        try:
            _users.create_user(username, password, role)
            _audit.log(current_user.username, 'USER_CREATED',
                       None, {'username': username, 'role': role})
            flash(f'User "{username}" created.', 'ok')
        except ValueError as exc:
            flash(str(exc), 'error')
        return redirect(url_for('users_page'))

    @app.route('/users/delete', methods=['POST'])
    @admin_required
    def delete_user_route():
        username = request.form.get('username', '').strip()
        if username == 'admin':
            flash('Cannot delete the admin account.', 'error')
            return redirect(url_for('users_page'))
        _users.delete_user(username)
        _audit.log(current_user.username, 'USER_DELETED',
                   {'username': username}, None)
        flash(f'User "{username}" deleted.', 'ok')
        return redirect(url_for('users_page'))

    # ── Views ─────────────────────────────────────────────────────────────────

    @app.route('/')
    @login_required
    def index():
        return render_template('index.html')

    # ── API — existing endpoints (now auth-protected) ─────────────────────────

    @app.route('/api/status')
    @login_required
    def api_status():
        data = engine.snapshot()
        data['config'] = {
            k: store.get(k)
            for k in ('plc_ip', 'plc_alarm_bit', 'moxa_ip', 'moxa_channel',
                      'moxa_pulse_interval', 'moxa_pulse_ack_bit')
        }
        if watchdog is not None:
            data['watchdog_ok'] = watchdog.is_ok
        return jsonify(data)

    @app.route('/api/alarms')
    @login_required
    def api_alarms():
        return jsonify(engine.get_alarms())

    @app.route('/api/config', methods=['POST'])
    @engineer_required
    def api_config():
        body    = request.get_json(force=True, silent=True) or {}
        allowed = {'plc_ip', 'plc_alarm_bit', 'moxa_ip', 'moxa_channel',
                   'moxa_pulse_interval', 'moxa_pulse_ack_bit'}
        updates = {k: v for k, v in body.items() if k in allowed}
        if not updates:
            return jsonify({'error': 'no valid fields'}), 400

        for ip_key in ('plc_ip', 'moxa_ip'):
            if ip_key in updates and not _valid_ip(str(updates[ip_key])):
                return jsonify({'error': f'invalid {ip_key}'}), 400

        if 'moxa_pulse_interval' in updates:
            try:
                v = float(updates['moxa_pulse_interval'])
                if not 0.2 <= v <= 60:
                    raise ValueError
                updates['moxa_pulse_interval'] = v
            except (ValueError, TypeError):
                return jsonify({'error': 'moxa_pulse_interval must be 0.2–60 seconds'}), 400

        if 'moxa_pulse_ack_bit' in updates:
            v = str(updates['moxa_pulse_ack_bit']).strip().upper()
            import re as _re
            if not _re.match(r'^[A-Z]+\d+\.\d{1,2}$', v):
                return jsonify({'error': 'moxa_pulse_ack_bit must be AREA WORD.BIT (e.g. W100.01)'}), 400
            updates['moxa_pulse_ack_bit'] = v

        if 'moxa_channel' in updates:
            try:
                ch = int(updates['moxa_channel'])
                if not 0 <= ch <= 7:
                    raise ValueError
                updates['moxa_channel'] = ch
            except (ValueError, TypeError):
                return jsonify({'error': 'moxa_channel must be integer 0–7'}), 400

        old = store.get_all()
        store.update(updates)
        for k, v in updates.items():
            _audit.log(current_user.username, f'CONFIG_{k.upper()}_CHANGED',
                       old.get(k), v)
        engine.sync()
        return jsonify({'ok': True})

    @app.route('/api/override', methods=['POST'])
    @engineer_required
    def api_override():
        body = request.get_json(force=True, silent=True) or {}
        val  = body.get('value', 'missing')
        if val not in (None, True, False):
            return jsonify({'error': 'value must be null, true, or false'}), 400
        old = store.get('manual_override')
        store.update({'manual_override': val})
        _audit.log(current_user.username, 'OVERRIDE_CHANGED', old, val)
        engine.sync()
        return jsonify({'ok': True})

    # ── API — new endpoints ───────────────────────────────────────────────────

    @app.route('/api/heartbeat')
    @login_required
    def api_heartbeat():
        snap = engine.snapshot()
        return jsonify({
            'status':     'ok',
            'uptime':     int(time.time() - _start),
            'last_cycle': snap.get('last_event_ts'),
            'watchdog':   watchdog.is_ok if watchdog is not None else None,
        })

    @app.route('/api/health')
    @login_required
    def api_health():
        snap = engine.snapshot()
        wd   = watchdog.is_ok if watchdog is not None else None
        return jsonify({
            'flask':    True,
            'engine':   wd if wd is not None else True,
            'watchdog': wd,
            'plc':      snap.get('plc_ok'),
            'moxa':     snap.get('moxa_ok'),
            'internet': snap.get('inet_state') == 'OK',
            'uptime':   int(time.time() - _start),
        })

    # ── Error handlers ────────────────────────────────────────────────────────

    @app.errorhandler(403)
    def _forbidden(_):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'forbidden'}), 403
        return render_template('login.html', error='Access denied for your role.'), 403

    return app
