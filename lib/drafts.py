from pathlib import Path

import config
from lib.state import read_json, write_json


def load_drafts() -> tuple[list, str | None]:
    data, error = read_json(config.PENDING_DRAFTS_FILE)
    if error:
        return [], error
    if data is None:
        return [], None
    return data if isinstance(data, list) else [], None


def get_pending_drafts() -> tuple[list, str | None]:
    drafts, error = load_drafts()
    if error:
        return [], error
    return [d for d in drafts if d.get("status") == "pending"], None


def approve_draft(draft_id: str) -> str | None:
    drafts, error = load_drafts()
    if error:
        return error

    target_draft = None
    for d in drafts:
        if d.get("id") == draft_id:
            if d.get("status") != "pending":
                return f"Draft {draft_id} is not pending (status: {d.get('status')})"
            d["status"] = "approved"
            target_draft = d
            break

    if not target_draft:
        return f"Draft {draft_id} not found"

    write_error = write_json(config.PENDING_DRAFTS_FILE, drafts)
    if write_error:
        return write_error

    _deliver_draft(target_draft)
    return None


def _deliver_draft(draft: dict):
    import subprocess
    chat_jid = draft.get("group_jid", "")
    text = draft.get("draft", "")
    wacli_store = draft.get("wacli_store", "")

    if not chat_jid or not text:
        return

    cmd = ["wacli", "send", "text", "--to", chat_jid, "--message", text]
    if wacli_store:
        cmd = ["wacli", "--store", wacli_store] + cmd[1:]

    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception:
        pass


def reject_draft(draft_id: str, reason: str = "") -> str | None:
    drafts, error = load_drafts()
    if error:
        return error

    found = False
    for d in drafts:
        if d.get("id") == draft_id:
            if d.get("status") != "pending":
                return f"Draft {draft_id} is not pending (status: {d.get('status')})"
            d["status"] = "rejected"
            if reason:
                d["reject_reason"] = reason
            found = True
            break

    if not found:
        return f"Draft {draft_id} not found"

    return write_json(config.PENDING_DRAFTS_FILE, drafts)


def edit_draft(draft_id: str, new_text: str) -> str | None:
    drafts, error = load_drafts()
    if error:
        return error

    found = False
    for d in drafts:
        if d.get("id") == draft_id:
            if d.get("status") != "pending":
                return f"Draft {draft_id} is not pending (status: {d.get('status')})"
            d["draft"] = new_text
            found = True
            break

    if not found:
        return f"Draft {draft_id} not found"

    return write_json(config.PENDING_DRAFTS_FILE, drafts)
