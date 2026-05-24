import json
import secrets
import time
from pathlib import Path

from flask_login import UserMixin

import config


class AdminUser(UserMixin):
    def __init__(self, user_id: str, name: str):
        self.id = user_id
        self.name = name


def _load_state() -> dict:
    if not config.ADMIN_STATE_FILE.exists():
        return {"tokens": {}, "sessions": {}}
    try:
        return json.loads(config.ADMIN_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"tokens": {}, "sessions": {}}


def _save_state(state: dict):
    from lib.state import write_json
    write_json(config.ADMIN_STATE_FILE, state)


def generate_invite_token(name: str) -> str:
    token = secrets.token_urlsafe(32)
    state = _load_state()
    state.setdefault("tokens", {})[token] = {
        "name": name,
        "created_at": time.time(),
        "used": False,
    }
    _save_state(state)
    return token


def consume_token(token: str) -> str | None:
    state = _load_state()
    tokens = state.get("tokens", {})
    if token not in tokens:
        return None
    if tokens[token].get("used"):
        return None
    name = tokens[token]["name"]
    tokens[token]["used"] = True
    tokens[token]["used_at"] = time.time()

    session_id = secrets.token_urlsafe(16)
    state.setdefault("sessions", {})[session_id] = {
        "name": name,
        "created_at": time.time(),
    }
    _save_state(state)
    return session_id


def load_user_by_session(session_id: str) -> AdminUser | None:
    state = _load_state()
    sessions = state.get("sessions", {})
    session = sessions.get(session_id)
    if not session:
        return None
    age = time.time() - session.get("created_at", 0)
    if age > 86400:
        del sessions[session_id]
        _save_state(state)
        return None
    return AdminUser(user_id=session_id, name=session["name"])


def is_tailscale_request(remote_addr: str) -> bool:
    return remote_addr.startswith("100.")
