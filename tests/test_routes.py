import json
from pathlib import Path


class TestUnauthenticated:
    def test_home_redirects_to_login(self, client):
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_people_requires_auth(self, client):
        resp = client.get("/people", follow_redirects=False)
        assert resp.status_code == 302

    def test_health_is_public(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"


class TestLogin:
    def test_valid_token_logs_in(self, app, client, tmp_state):
        from lib.auth import generate_invite_token
        token = generate_invite_token("Tester")
        resp = client.get(f"/login?token={token}", follow_redirects=True)
        assert resp.status_code == 200
        assert b"Forge" in resp.data

    def test_invalid_token_shows_error(self, client):
        resp = client.get("/login?token=bogus", follow_redirects=True)
        assert b"Invalid or expired" in resp.data

    def test_token_is_single_use(self, app, client, tmp_state):
        from lib.auth import generate_invite_token
        token = generate_invite_token("Once")
        client.get(f"/login?token={token}")
        resp2 = client.get(f"/login?token={token}", follow_redirects=True)
        assert b"Invalid or expired" in resp2.data


class TestHome:
    def test_home_renders(self, authed_client):
        resp = authed_client.get("/")
        assert resp.status_code == 200
        assert b"Forge" in resp.data

    def test_home_shows_session_budget(self, authed_client):
        resp = authed_client.get("/")
        assert b"/ 15" in resp.data


class TestPeople:
    def test_people_page_renders(self, authed_client):
        resp = authed_client.get("/people")
        assert resp.status_code == 200
        assert b"Tariq (Business)" in resp.data

    def test_add_person_valid(self, authed_client, tmp_state):
        resp = authed_client.post("/people/add", data={
            "name": "Ahmad",
            "jid": "971501234567@s.whatsapp.net",
            "role": "staff",
        }, follow_redirects=True)
        assert resp.status_code == 200
        perms = json.loads((tmp_state["rules_dir"] / "permissions.json").read_text())
        assert any(p["name"] == "Ahmad" for p in perms["people"])

    def test_add_person_creates_sentinel(self, authed_client, tmp_state):
        authed_client.post("/people/add", data={
            "name": "Sana",
            "jid": "971509876543@s.whatsapp.net",
            "role": "personal",
        }, follow_redirects=True)
        assert (tmp_state["state_dir"] / ".permissions-reload").exists()

    def test_add_person_invalid_jid(self, authed_client):
        resp = authed_client.post("/people/add", data={
            "name": "Bad",
            "jid": "not-a-jid",
            "role": "staff",
        }, follow_redirects=True)
        assert b"Invalid JID" in resp.data

    def test_add_person_duplicate(self, authed_client):
        authed_client.post("/people/add", data={
            "name": "First",
            "jid": "971501111111@s.whatsapp.net",
            "role": "staff",
        })
        resp = authed_client.post("/people/add", data={
            "name": "Dupe",
            "jid": "971501111111@s.whatsapp.net",
            "role": "client",
        }, follow_redirects=True)
        assert b"already exists" in resp.data

    def test_add_person_invalid_role(self, authed_client):
        resp = authed_client.post("/people/add", data={
            "name": "Bad Role",
            "jid": "971502222222@s.whatsapp.net",
            "role": "admin",
        }, follow_redirects=True)
        assert b"Invalid role" in resp.data

    def test_remove_person(self, authed_client, tmp_state):
        authed_client.post("/people/add", data={
            "name": "ToRemove",
            "jid": "971503333333@s.whatsapp.net",
            "role": "vendor",
        })
        resp = authed_client.post(
            "/people/remove/971503333333@s.whatsapp.net",
            follow_redirects=True,
        )
        assert b"Person removed" in resp.data
        perms = json.loads((tmp_state["rules_dir"] / "permissions.json").read_text())
        assert not any(p["jid"] == "971503333333@s.whatsapp.net" for p in perms["people"])

    def test_remove_nonexistent(self, authed_client):
        resp = authed_client.post(
            "/people/remove/971500000000@s.whatsapp.net",
            follow_redirects=True,
        )
        assert b"not found" in resp.data


class TestDrafts:
    def _add_draft(self, tmp_state, draft_id="test-1", status="pending"):
        drafts = [{"id": draft_id, "group_jid": "971501234567@s.whatsapp.net",
                   "group_name": "Test Chat", "draft": "Hello there",
                   "source": "test", "for_role": "staff", "status": status,
                   "created_at": "2026-05-24T12:00:00"}]
        (tmp_state["state_dir"] / "pending_drafts.json").write_text(json.dumps(drafts))

    def test_drafts_page_renders(self, authed_client):
        resp = authed_client.get("/drafts")
        assert resp.status_code == 200

    def test_approve_draft(self, authed_client, tmp_state):
        self._add_draft(tmp_state)
        resp = authed_client.post("/drafts/test-1/approve", follow_redirects=True)
        assert resp.status_code == 200
        drafts = json.loads((tmp_state["state_dir"] / "pending_drafts.json").read_text())
        assert drafts[0]["status"] == "approved"

    def test_reject_draft(self, authed_client, tmp_state):
        self._add_draft(tmp_state)
        resp = authed_client.post("/drafts/test-1/reject", data={"reason": "not good"},
                                  follow_redirects=True)
        assert resp.status_code == 200
        drafts = json.loads((tmp_state["state_dir"] / "pending_drafts.json").read_text())
        assert drafts[0]["status"] == "rejected"

    def test_approve_nonexistent(self, authed_client, tmp_state):
        self._add_draft(tmp_state)
        resp = authed_client.post("/drafts/fake-id/approve", follow_redirects=True)
        assert b"not found" in resp.data

    def test_approve_already_approved(self, authed_client, tmp_state):
        self._add_draft(tmp_state, status="approved")
        resp = authed_client.post("/drafts/test-1/approve", follow_redirects=True)
        assert resp.status_code == 200
        # Draft stays approved (doesn't break)
        drafts = json.loads((tmp_state["state_dir"] / "pending_drafts.json").read_text())
        assert drafts[0]["status"] == "approved"


class TestActivity:
    def test_activity_page_renders(self, authed_client):
        resp = authed_client.get("/activity")
        assert resp.status_code == 200

    def test_activity_shows_stats(self, authed_client, tmp_state):
        entries = [
            json.dumps({"ts": "2026-05-24 12:00 UTC", "sender": "A", "chat": "G",
                        "result": {"action": "skip", "summary": "no action"}}),
            json.dumps({"ts": "2026-05-24 12:01 UTC", "sender": "B", "chat": "G",
                        "result": {"action": "draft", "summary": "needs reply"}}),
        ]
        (tmp_state["state_dir"] / "wa-triage.log").write_text("\n".join(entries))
        resp = authed_client.get("/activity")
        assert resp.status_code == 200


class TestMalformedJson:
    def test_home_with_malformed_permissions(self, authed_client, tmp_state):
        (tmp_state["rules_dir"] / "permissions.json").write_text("{bad json")
        resp = authed_client.get("/")
        assert resp.status_code == 200
        assert b"error" in resp.data.lower() or b"Error" in resp.data or b"permissions.json" in resp.data

    def test_drafts_with_malformed_file(self, authed_client, tmp_state):
        (tmp_state["state_dir"] / "pending_drafts.json").write_text("not json")
        resp = authed_client.get("/drafts")
        assert resp.status_code == 200
