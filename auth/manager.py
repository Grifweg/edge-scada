"""
auth/manager.py — Flask-Login setup and User model.
"""
from __future__ import annotations

from flask_login import LoginManager, UserMixin

login_manager = LoginManager()
login_manager.login_view         = 'login_page'
login_manager.login_message      = 'Please log in.'
login_manager.session_protection = 'basic'


class User(UserMixin):
    def __init__(self, username: str, record: dict) -> None:
        self.id                   = username
        self.username             = username
        self.role                 = record.get('role', 'operator')
        self.must_change_password = record.get('must_change_password', False)

    @property
    def is_admin(self) -> bool:
        return self.role == 'administrator'

    @property
    def is_engineer(self) -> bool:
        return self.role in ('administrator', 'engineer')


@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    from auth.users import UserStore
    record = UserStore().get_user(user_id)
    return User(user_id, record) if record else None
