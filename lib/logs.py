import json
from datetime import date, datetime
from pathlib import Path

import config


def get_triage_entries(limit: int = 50) -> list[dict]:
    if not config.TRIAGE_LOG_FILE.exists():
        return []
    try:
        lines = config.TRIAGE_LOG_FILE.read_text().strip().split("\n")
        entries = []
        for line in reversed(lines):
            if len(entries) >= limit:
                break
            try:
                raw = json.loads(line)
                result = raw.get("result", {})
                entries.append({
                    "ts": raw.get("ts", ""),
                    "sender": raw.get("sender", "Unknown"),
                    "chat": raw.get("chat", ""),
                    "action": result.get("action", raw.get("action", "skip")),
                    "reason": result.get("summary", raw.get("reason", "")),
                    "tier": raw.get("tier", ""),
                })
            except json.JSONDecodeError:
                continue
        return entries
    except Exception:
        return []


def get_today_session_count() -> int:
    if not config.AGENT_LOG_FILE.exists():
        return 0
    today_str = date.today().isoformat()
    try:
        count = 0
        with open(config.AGENT_LOG_FILE, "r") as f:
            for line in f:
                if today_str in line:
                    count += 1
        return count
    except Exception:
        return 0


def get_bridge_status() -> dict:
    if not config.BRIDGE_LOG_FILE.exists():
        return {"status": "unknown", "last_activity": None}
    try:
        lines = config.BRIDGE_LOG_FILE.read_text().strip().split("\n")
        if not lines:
            return {"status": "unknown", "last_activity": None}
        last_line = lines[-1]
        return {"status": "healthy", "last_activity": last_line[:19]}
    except Exception:
        return {"status": "error", "last_activity": None}


def get_today_stats() -> dict:
    entries = get_triage_entries(limit=500)
    today_str = date.today().strftime("%Y-%m-%d")
    today_entries = [e for e in entries if today_str in e.get("ts", "")]
    skip = sum(1 for e in today_entries if e.get("action") == "skip")
    notify = sum(1 for e in today_entries if e.get("action") == "notify")
    draft = sum(1 for e in today_entries if e.get("action") == "draft")
    return {
        "total": len(today_entries),
        "skip": skip,
        "notify": notify,
        "draft": draft,
    }
