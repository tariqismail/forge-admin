import json
import os
import tempfile
from pathlib import Path

import pytest

os.environ["FLASK_ENV"] = "development"


@pytest.fixture
def tmp_state(tmp_path):
    """Create temp state files mimicking Forge's directory structure."""
    rules_dir = tmp_path / "forge-rules"
    state_dir = tmp_path / "forge-state"
    rules_dir.mkdir()
    state_dir.mkdir()

    permissions = {
        "version": 1,
        "roles": {
            "commander": {"answer": "all", "action": "all", "reply": "auto"},
            "staff": {"answer": ["tb_project_status", "schedule"], "action": ["create_note"], "reply": "auto"},
            "client": {"answer": ["own_project_status"], "action": [], "reply": "draft"},
            "vendor": {"answer": ["po_status"], "action": [], "reply": "draft"},
            "personal": {"answer": ["general", "schedule"], "action": ["create_reminder"], "reply": "draft"},
        },
        "people": [
            {"jid": "971567819398@s.whatsapp.net", "name": "Tariq (Business)", "role": "commander"},
        ],
    }

    (rules_dir / "permissions.json").write_text(json.dumps(permissions, indent=2))
    (state_dir / "pending_drafts.json").write_text("[]")
    (state_dir / "wa-triage.log").write_text("")
    (state_dir / "wa-bridge-agent.log").write_text("")
    (state_dir / "wa-bridge.log").write_text("")

    return {"rules_dir": rules_dir, "state_dir": state_dir}


@pytest.fixture
def app(tmp_state):
    """Create a test Flask app pointing at temp state."""
    import config
    config.FORGE_RULES_DIR = tmp_state["rules_dir"]
    config.FORGE_STATE_DIR = tmp_state["state_dir"]
    config.PERMISSIONS_FILE = tmp_state["rules_dir"] / "permissions.json"
    config.PENDING_DRAFTS_FILE = tmp_state["state_dir"] / "pending_drafts.json"
    config.TRIAGE_LOG_FILE = tmp_state["state_dir"] / "wa-triage.log"
    config.AGENT_LOG_FILE = tmp_state["state_dir"] / "wa-bridge-agent.log"
    config.BRIDGE_LOG_FILE = tmp_state["state_dir"] / "wa-bridge.log"
    config.RELOAD_SENTINEL = tmp_state["state_dir"] / ".permissions-reload"
    config.ADMIN_STATE_FILE = tmp_state["state_dir"] / "forge-admin-state.json"
    config.SECRET_KEY = "test-secret"
    config.SESSION_COOKIE_SECURE = False

    from app import app as flask_app
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    flask_app.config["SECRET_KEY"] = "test-secret"
    flask_app.config["SESSION_COOKIE_SECURE"] = False

    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def authed_client(app, tmp_state):
    """Client with an active session (bypasses login)."""
    from lib.auth import generate_invite_token, consume_token
    token = generate_invite_token("Test User")
    session_id = consume_token(token)

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = session_id
    return client
