import re
from pathlib import Path

from lib.state import read_json, write_json, touch_sentinel
from config import PERMISSIONS_FILE, RELOAD_SENTINEL

JID_PATTERN = re.compile(r"^\d{10,15}@s\.whatsapp\.net$")


def validate_jid(jid: str) -> str | None:
    if not jid:
        return "JID is required"
    if not JID_PATTERN.match(jid):
        return "Invalid JID format. Expected: 971XXXXXXXXX@s.whatsapp.net"
    return None


def load_permissions() -> tuple[dict, str | None]:
    data, error = read_json(PERMISSIONS_FILE)
    if error:
        return {"roles": {}, "people": {}}, error
    if data is None:
        return {"roles": {}, "people": {}}, None

    roles = {k: v for k, v in data.get("roles", {}).items()}
    people = {}
    for entry in data.get("people", []):
        jid = entry.get("jid")
        if jid and not jid.startswith("9715X"):
            people[jid] = entry
    return {"roles": roles, "people": people}, None


def resolve_person(jid: str, permissions: dict) -> dict | None:
    person = permissions.get("people", {}).get(jid)
    if not person:
        return None
    role_name = person.get("role", "")
    role = permissions.get("roles", {}).get(role_name, {})
    return {
        "jid": jid,
        "name": person.get("name", jid),
        "role": role_name,
        "answer": person.get("answer", role.get("answer", [])),
        "action": person.get("action", role.get("action", [])),
        "reply": person.get("reply", role.get("reply", "draft")),
        "scope": person.get("scope", role.get("scope")),
        "project": person.get("project"),
        "deny": person.get("deny", []),
    }


def add_person(name: str, jid: str, role: str, project: str | None = None) -> str | None:
    jid_error = validate_jid(jid)
    if jid_error:
        return jid_error

    data, error = read_json(PERMISSIONS_FILE)
    if error:
        return f"Cannot read permissions.json: {error}"
    if data is None:
        return "permissions.json does not exist"

    valid_roles = set(data.get("roles", {}).keys())
    if role not in valid_roles:
        return f"Invalid role '{role}'. Valid: {', '.join(sorted(valid_roles))}"

    people = data.get("people", [])
    for p in people:
        if p.get("jid") == jid:
            return f"Person with JID {jid} already exists"

    entry = {"jid": jid, "name": name, "role": role}
    if project:
        entry["project"] = project

    people.append(entry)
    data["people"] = people

    write_error = write_json(PERMISSIONS_FILE, data)
    if write_error:
        return write_error

    touch_sentinel(RELOAD_SENTINEL)
    return None


def remove_person(jid: str) -> str | None:
    data, error = read_json(PERMISSIONS_FILE)
    if error:
        return f"Cannot read permissions.json: {error}"
    if data is None:
        return "permissions.json does not exist"

    people = data.get("people", [])
    original_len = len(people)
    data["people"] = [p for p in people if p.get("jid") != jid]

    if len(data["people"]) == original_len:
        return f"Person with JID {jid} not found"

    write_error = write_json(PERMISSIONS_FILE, data)
    if write_error:
        return write_error

    touch_sentinel(RELOAD_SENTINEL)
    return None
