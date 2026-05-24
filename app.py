from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, Response
from flask_wtf import CSRFProtect
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from datetime import timedelta
import json
import time

import config
from lib.auth import AdminUser, consume_token, load_user_by_session, generate_invite_token, is_tailscale_request
from lib.permissions import load_permissions, add_person, remove_person
from lib.drafts import get_pending_drafts, approve_draft, reject_draft, edit_draft, load_drafts
from lib.logs import get_triage_entries, get_today_session_count, get_bridge_status, get_today_stats
from lib.state import read_json

app = Flask(__name__)
app.config.from_object(config)
app.config["WTF_CSRF_TIME_LIMIT"] = None

import os
if os.environ.get("FLASK_ENV") != "development":
    app.config["SESSION_COOKIE_SECURE"] = True
else:
    app.config["SESSION_COOKIE_SECURE"] = False

csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"


@login_manager.user_loader
def user_loader(session_id):
    return load_user_by_session(session_id)


@app.before_request
def tailscale_auto_login():
    if current_user.is_authenticated:
        return
    if is_tailscale_request(request.remote_addr):
        ts_user = request.headers.get("Tailscale-User-Login")
        if ts_user:
            user = AdminUser(user_id="tailscale-" + ts_user, name=ts_user)
            login_user(user, remember=True, duration=timedelta(hours=24))


@app.route("/")
@login_required
def home():
    permissions, perm_error = load_permissions()
    drafts, draft_error = get_pending_drafts()
    triage = get_triage_entries(limit=10)
    sessions_today = get_today_session_count()
    bridge = get_bridge_status()
    stats = get_today_stats()

    return render_template("home.html",
        permissions=permissions,
        perm_error=perm_error,
        drafts=drafts,
        draft_error=draft_error,
        triage=triage,
        sessions_today=sessions_today,
        bridge=bridge,
        stats=stats,
    )


@app.route("/people")
@login_required
def people():
    permissions, perm_error = load_permissions()
    return render_template("people.html",
        permissions=permissions,
        perm_error=perm_error,
    )


@app.route("/people/add", methods=["POST"])
@login_required
def people_add():
    name = request.form.get("name", "").strip()
    jid = request.form.get("jid", "").strip()
    role = request.form.get("role", "").strip()
    project = request.form.get("project", "").strip() or None

    if not name:
        flash("Name is required", "error")
        return redirect(url_for("people"))

    error = add_person(name, jid, role, project)
    if error:
        flash(error, "error")
    else:
        flash(f"Added {name} as {role}", "success")

    return redirect(url_for("people"))


@app.route("/people/remove/<path:jid>", methods=["POST"])
@login_required
def people_remove(jid):
    error = remove_person(jid)
    if error:
        flash(error, "error")
    else:
        flash("Person removed", "success")
    return redirect(url_for("people"))


@app.route("/drafts")
@login_required
def drafts():
    all_drafts, error = load_drafts()
    pending = [d for d in all_drafts if d.get("status") == "pending"]
    sent = [d for d in all_drafts if d.get("status") == "approved"]
    discarded = [d for d in all_drafts if d.get("status") == "rejected"]
    return render_template("drafts.html",
        pending=pending,
        sent=sent,
        discarded=discarded,
        error=error,
    )


@app.route("/drafts/<draft_id>/approve", methods=["POST"])
@login_required
def draft_approve(draft_id):
    error = approve_draft(draft_id)
    if error:
        flash(error, "error")
    else:
        flash("Draft approved. Will be sent on next poll.", "success")
    return redirect(url_for("drafts"))


@app.route("/drafts/<draft_id>/reject", methods=["POST"])
@login_required
def draft_reject(draft_id):
    reason = request.form.get("reason", "")
    error = reject_draft(draft_id, reason)
    if error:
        flash(error, "error")
    else:
        flash("Draft discarded", "success")
    return redirect(url_for("drafts"))


@app.route("/drafts/<draft_id>/edit", methods=["POST"])
@login_required
def draft_edit(draft_id):
    new_text = request.form.get("text", "").strip()
    if not new_text:
        flash("Draft text cannot be empty", "error")
        return redirect(url_for("drafts"))
    error = edit_draft(draft_id, new_text)
    if error:
        flash(error, "error")
    else:
        flash("Draft updated", "success")
    return redirect(url_for("drafts"))


@app.route("/activity")
@login_required
def activity():
    triage = get_triage_entries(limit=100)
    stats = get_today_stats()
    return render_template("activity.html", triage=triage, stats=stats)


@app.route("/health")
@csrf.exempt
def health():
    bridge = get_bridge_status()
    sessions = get_today_session_count()
    drafts, _ = get_pending_drafts()
    return jsonify({
        "status": "ok",
        "bridge": bridge,
        "sessions_today": sessions,
        "pending_drafts": len(drafts),
    })


@app.route("/login")
def login():
    token = request.args.get("token")
    if not token:
        return render_template("login.html")

    session_id = consume_token(token)
    if not session_id:
        flash("Invalid or expired invite link", "error")
        return render_template("login.html")

    user = load_user_by_session(session_id)
    if user:
        login_user(user, remember=True, duration=timedelta(hours=24))
        return redirect(url_for("home"))

    flash("Login failed", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/raw/<path:filename>")
@login_required
def raw_editor(filename):
    filepath = config.FORGE_RULES_DIR / filename
    if not filepath.exists():
        filepath = config.FORGE_STATE_DIR / filename
    if not filepath.exists():
        flash(f"File not found: {filename}", "error")
        return redirect(url_for("home"))
    content = filepath.read_text()
    return render_template("raw_editor.html", filename=filename, content=content)


@app.route("/raw/<path:filename>/save", methods=["POST"])
@login_required
def raw_save(filename):
    filepath = config.FORGE_RULES_DIR / filename
    if not filepath.exists():
        filepath = config.FORGE_STATE_DIR / filename
    content = request.form.get("content", "")
    try:
        json.loads(content)
    except json.JSONDecodeError as e:
        flash(f"Invalid JSON: {e}", "error")
        return render_template("raw_editor.html", filename=filename, content=content)

    from lib.state import write_json
    import tempfile, os
    fd, tmp = tempfile.mkstemp(dir=filepath.parent, suffix=".tmp")
    os.write(fd, content.encode("utf-8"))
    os.close(fd)
    os.rename(tmp, filepath)

    if "permissions" in filename:
        from lib.state import touch_sentinel
        touch_sentinel(config.RELOAD_SENTINEL)

    flash("Saved", "success")
    return redirect(url_for("home"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True)
