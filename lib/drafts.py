from pathlib import Path

from lib.state import read_json, write_json
from config import PENDING_DRAFTS_FILE


def load_drafts() -> tuple[list, str | None]:
    data, error = read_json(PENDING_DRAFTS_FILE)
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

    found = False
    for d in drafts:
        if d.get("id") == draft_id:
            if d.get("status") != "pending":
                return f"Draft {draft_id} is not pending (status: {d.get('status')})"
            d["status"] = "approved"
            found = True
            break

    if not found:
        return f"Draft {draft_id} not found"

    return write_json(PENDING_DRAFTS_FILE, drafts)


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

    return write_json(PENDING_DRAFTS_FILE, drafts)


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

    return write_json(PENDING_DRAFTS_FILE, drafts)
