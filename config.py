import os
from pathlib import Path

FORGE_STATE_DIR = Path(os.environ.get("FORGE_STATE_DIR", Path.home() / "forge" / "forge-state"))
FORGE_RULES_DIR = Path(os.environ.get("FORGE_RULES_DIR", Path.home() / "forge" / "forge-rules"))

PERMISSIONS_FILE = FORGE_RULES_DIR / "permissions.json"
PENDING_DRAFTS_FILE = FORGE_STATE_DIR / "pending_drafts.json"
TRIAGE_LOG_FILE = FORGE_STATE_DIR / "wa-triage.log"
AGENT_LOG_FILE = FORGE_STATE_DIR / "wa-bridge-agent.log"
BRIDGE_LOG_FILE = FORGE_STATE_DIR / "wa-bridge.log"
RELOAD_SENTINEL = FORGE_STATE_DIR / ".permissions-reload"

ADMIN_STATE_FILE = Path(os.environ.get("ADMIN_STATE_FILE", FORGE_STATE_DIR / "forge-admin-state.json"))

SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-in-production")
SESSION_COOKIE_SECURE = os.environ.get("FLASK_ENV") != "development"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
PERMANENT_SESSION_LIFETIME = 86400
