#!/usr/bin/env python3
"""
wa-bridge.py — wacli SQLite poller → openclaw agent router

Polls wacli.db every 2 seconds for new inbound messages and routes each
message through the appropriate tier:

  Tier 1 — /forge commands (Tariq or whitelisted sender)
      → Claude via openclaw agent. Direct command, full tool access.

  Tier 2 — True Build bound groups (tb-ops, tb-admin)
      → Ollama pre-screen (qwen3.6, local, free).
        If pre-screen says "needs_draft" → Claude via openclaw (MCP tools).
        If pre-screen says no action needed → silently skip.

  Tier 3 — Personal / unbound groups and DMs
      → Ollama triage (qwen3.6, local, free).
        If triage says "notify" → Telegram direct (Telegram Bot API, no openclaw).
        If triage says "skip" → silently ignored.

Safety limits (OOM prevention):
  MAX_CONCURRENT_CLAUDE — at most this many openclaw Node.js processes at once.
  MAX_CONCURRENT_OLLAMA — at most this many concurrent Ollama triage calls.
  MAX_MESSAGE_AGE_SECONDS — skip messages older than this (historical backfill guard).
"""
import base64
import concurrent.futures
import json
import logging
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# ── Config ───────────────────────────────────────────────────────────────────
OPENCLAW_CONFIG = Path.home() / ".openclaw" / "openclaw.json"
WHITELIST_FILE  = Path.home() / "forge" / "forge-rules" / "forge_commander_whitelist.yaml"
PERMISSIONS_FILE = Path.home() / "forge" / "forge-rules" / "permissions.json"

# gbrain — long-term semantic memory. Bridge calls it by absolute path (launchd PATH
# does not include ~/.bun/bin). Recall is injected only into OPERATOR prompts (never
# delegated roles — Tariq's broader memory must not leak into a client/vendor reply).
GBRAIN          = str(Path.home() / ".bun" / "bin" / "gbrain")
GBRAIN_TIMEOUT  = 6
AGENT_LOG       = Path.home() / "forge" / "forge-state" / "wa-bridge-agent.log"
TRIAGE_LOG      = Path.home() / "forge" / "forge-state" / "wa-triage.log"

POLL_INTERVAL   = 2     # seconds between DB polls
BATCH_LIMIT     = 50    # messages per poll
FORGE_PREFIX    = "/forge"
# Both trigger Forge. "@forge" is the natural mention form; "/forge" the command form.
FORGE_TRIGGERS  = ("/forge", "@forge")

# ── WhatsApp accounts ─────────────────────────────────────────────────────────
# Each account is polled independently with its own cursor and wacli store.
ACCOUNTS = [
    {
        "label":       "personal",
        "db":          Path.home() / ".wacli" / "wacli.db",
        "store":       str(Path.home() / ".wacli"),
        "cursor_file": Path.home() / "forge" / "forge-state" / "wa-bridge-cursor.json",
    },
    {
        "label":       "business",
        "db":          Path.home() / ".wacli-business" / "wacli.db",
        "store":       str(Path.home() / ".wacli-business"),
        "cursor_file": Path.home() / "forge" / "forge-state" / "wa-bridge-cursor-business.json",
    },
]

# ── Thread context ─────────────────────────────────────────────────────────────
CONTEXT_DIR              = Path.home() / "forge" / "forge-state" / "contexts"
CONTEXT_MAX_MESSAGES     = 10    # max turns kept per chat
CONTEXT_MAX_AGE_SECONDS  = 1800  # 30 min — context expires after this

# ── Safety limits ─────────────────────────────────────────────────────────────
MAX_CONCURRENT_CLAUDE  = 3
MAX_CONCURRENT_OLLAMA  = 2
# 240s: a cold gateway-runtime start is ~54s; add real task + tool calls on top.
# The old 120s cap was tripping mid-task on cold starts (the "timed out after 120s"
# errors). Warm calls still return in ~8s, so this only raises the ceiling.
AGENT_TIMEOUT_SECONDS  = 240
OLLAMA_TIMEOUT_SECONDS = 60          # triage; model stays resident via keep_alive
OLLAMA_DRAFT_TIMEOUT_SECONDS = 120  # drafting on qwen3.6 (resident, no swap-thrash)
MAX_MESSAGE_AGE_SECONDS    = 1800   # 30 min — personal / unbound groups
MAX_TB_MESSAGE_AGE_SECONDS = 14400  # 4 hours — TB-routed groups (survive reboots)

# ── Reliability settings ──────────────────────────────────────────────────────
ROUTING_RELOAD_INTERVAL = 300.0   # reload routing table every 5 min
HEALTH_CHECK_INTERVAL   = 60      # check wacli DB health once per minute
# Zombie-sync detection (process alive but receiving nothing — the 2026-05-23 failure).
SYNC_STALE_ALERT_MIN    = 90      # an account's newest msg older than this = suspicious
SYNC_SYSTEM_FRESH_MIN   = 25      # ...but only alert if SOME account got traffic this recently

# Billing error patterns — detected in openclaw stderr to trigger Ollama fallback
BILLING_ERROR_PATTERNS = (
    "LLM request rejected",
    "extra usage",
    "suspending lanes",
    "billing",
    "cooldown",
)

# ── Ollama ────────────────────────────────────────────────────────────────────
# Single resident model strategy (M2 Ultra 64GB): qwen3.6 (~34GB resident) handles
# both triage AND draft fallback. qwen2.5:72b (47GB) is deliberately NOT used in the
# hot path — loading it evicts qwen3.6 and the swap-thrash caused the triage timeouts.
# keep_alive pins qwen3.6 in memory so it never reloads from disk between messages.
OLLAMA_URL           = "http://localhost:11434"
OLLAMA_TRIAGE_MODEL  = "qwen3.6:latest"    # fast 23GB MoE — triage and pre-screen
OLLAMA_DRAFT_MODEL   = "qwen3.6:latest"    # same resident model — drafting fallback when Claude fails
OLLAMA_VISION_MODEL  = "moondream:latest"  # lightweight 1.7GB — image description for Ollama fallback
OLLAMA_KEEP_ALIVE    = "24h"               # keep the triage/draft model resident; kills reload latency

# ── Telegram (direct Bot API — no openclaw dependency) ────────────────────────
# Credentials loaded from ~/.secrets (FORGE_TELEGRAM_TOKEN / FORGE_TELEGRAM_CHAT_ID).
# Falls back to env vars, then hard defaults that will be removed in a future pass.
def _load_secrets_file() -> dict[str, str]:
    p = Path.home() / ".secrets"
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip("\"'")
    return out

_secrets = _load_secrets_file()
TELEGRAM_TOKEN   = _secrets.get("FORGE_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = _secrets.get("FORGE_TELEGRAM_CHAT_ID", "")

# ── Logging ───────────────────────────────────────────────────────────────────
log_path = Path.home() / "forge" / "forge-state" / "wa-bridge.log"
_handlers = [logging.FileHandler(log_path)]
if sys.stdout.isatty():
    _handlers.append(logging.StreamHandler(sys.stdout))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("wa-bridge")

# ── Thread pools ──────────────────────────────────────────────────────────────
_claude_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_CLAUDE, thread_name_prefix="claude"
)
_ollama_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_OLLAMA, thread_name_prefix="ollama"
)

_health_state: dict = {}  # tracks per-account sync health for alerting


# ── Routing table ─────────────────────────────────────────────────────────────
def load_routing_table() -> dict[str, str]:
    with OPENCLAW_CONFIG.open() as f:
        config = json.load(f)
    table = {}
    for binding in config.get("bindings", []):
        if binding.get("type") != "route":
            continue
        match = binding.get("match", {})
        if match.get("channel") != "whatsapp":
            continue
        peer = match.get("peer", {})
        if peer.get("kind") == "group" and peer.get("id"):
            table[peer["id"]] = binding["agentId"]
    return table


# ── Whitelist ─────────────────────────────────────────────────────────────────
def load_whitelist() -> dict[str, dict]:
    if not WHITELIST_FILE.exists():
        return {}
    try:
        content = WHITELIST_FILE.read_text()
        whitelist: dict[str, dict] = {}
        current: dict = {}
        current_jid: str | None = None
        for line in content.splitlines():
            s = line.strip()
            if s.startswith("- jid:"):
                if current_jid:
                    whitelist[current_jid] = current
                current_jid = s.split(":", 1)[1].strip().strip("\"'")
                current = {}
            elif current_jid and "name:" in s:
                current["name"] = s.split(":", 1)[1].strip().strip("\"'")
            elif current_jid and "access:" in s:
                current["access"] = s.split(":", 1)[1].strip().strip("\"'")
            elif current_jid and "allowed_commands:" in s:
                current["allowed_commands"] = []
            elif current_jid and "allowed_commands" in current and s.startswith("- "):
                current.setdefault("allowed_commands", []).append(
                    s[2:].strip().strip("\"'")
                )
        if current_jid:
            whitelist[current_jid] = current
        return whitelist
    except Exception as e:
        log.warning("Failed to load whitelist: %s", e)
        return {}


# ── Permissions (roles + people) ──────────────────────────────────────────────
def load_permissions() -> dict:
    """Load permissions.json → {'roles': {...}, 'people': {jid: entry}}.

    Returns empty roles/people on any error so a malformed file fails CLOSED
    (nobody gets elevated access) rather than crashing the bridge.
    """
    if not PERMISSIONS_FILE.exists():
        return {"roles": {}, "people": {}}
    try:
        data = json.loads(PERMISSIONS_FILE.read_text())
        roles = {k: v for k, v in data.get("roles", {}).items()}
        people = {}
        for entry in data.get("people", []):
            jid = entry.get("jid")
            if jid and not jid.startswith("9715X"):  # skip placeholder examples
                people[jid] = entry
        return {"roles": roles, "people": people}
    except Exception as e:
        log.warning("Failed to load permissions.json: %s", e)
        return {"roles": {}, "people": {}}


def resolve_person(jid: str, permissions: dict) -> dict | None:
    """Resolve a sender JID to their effective permission set, or None if not allowlisted.

    Merges the assigned role's defaults with any per-person overrides
    (deny / project / explicit answer/action/reply on the person entry).
    """
    person = permissions.get("people", {}).get(jid)
    if not person:
        return None
    role_name = person.get("role", "")
    role = permissions.get("roles", {}).get(role_name, {})
    eff = {
        "jid": jid,
        "name": person.get("name", jid),
        "role": role_name,
        "answer": person.get("answer", role.get("answer", [])),
        "action": person.get("action", role.get("action", [])),
        "reply": person.get("reply", role.get("reply", "draft")),
        "scope": person.get("scope", role.get("scope")),
        "project": person.get("project"),
        "deny": person.get("deny", []),
        "deny_action": role.get("deny_action", []),
    }
    return eff


# ── Cursor ────────────────────────────────────────────────────────────────────
def load_cursor(cursor_file: Path) -> int:
    if cursor_file.exists():
        try:
            return int(json.loads(cursor_file.read_text()).get("last_rowid", 0))
        except Exception:
            pass
    return -1


def save_cursor(rowid: int, cursor_file: Path) -> None:
    tmp = cursor_file.with_suffix(".tmp")
    tmp.write_text(json.dumps({"last_rowid": rowid}))
    tmp.rename(cursor_file)


# ── Message fetching ──────────────────────────────────────────────────────────
QUERY = """
SELECT
    rowid,
    chat_jid,
    COALESCE(chat_name, '') AS chat_name,
    msg_id,
    COALESCE(sender_jid, chat_jid) AS sender_jid,
    COALESCE(sender_name, '') AS sender_name,
    ts,
    from_me,
    COALESCE(display_text, text, '') AS text,
    COALESCE(text, '') AS raw_text,
    COALESCE(media_type, '') AS media_type,
    COALESCE(media_caption, '') AS media_caption,
    COALESCE(filename, '') AS filename,
    COALESCE(local_path, '') AS local_path
FROM messages
WHERE rowid > ?
  AND revoked = 0
  AND deleted_for_me = 0
  AND reaction_to_id IS NULL
  AND (
    from_me = 0
    OR (from_me = 1 AND (
         LOWER(COALESCE(text, '')) LIKE '/forge%'
      OR LOWER(COALESCE(text, '')) LIKE '@forge%'
      OR LOWER(COALESCE(display_text, '')) LIKE '/forge%'
      OR LOWER(COALESCE(display_text, '')) LIKE '@forge%'
    ))
  )
ORDER BY rowid ASC
LIMIT ?
"""


def fetch_new_messages(conn: sqlite3.Connection, after_rowid: int) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(QUERY, (after_rowid, BATCH_LIMIT)).fetchall()]


# ── Thread context ────────────────────────────────────────────────────────────

def _context_path(chat_jid: str) -> Path:
    """Return the context file path for a given chat JID."""
    CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w]", "_", chat_jid)
    return CONTEXT_DIR / f"{safe}.json"


def load_context(chat_jid: str) -> list[dict]:
    """Load recent conversation turns for a chat, pruned to the time window."""
    path = _context_path(chat_jid)
    if not path.exists():
        return []
    try:
        turns = json.loads(path.read_text())
        cutoff = time.time() - CONTEXT_MAX_AGE_SECONDS
        turns = [t for t in turns if t.get("ts", 0) >= cutoff]
        return turns[-CONTEXT_MAX_MESSAGES:]
    except Exception:
        return []


def save_to_context(chat_jid: str, role: str, text: str) -> None:
    """Append a turn to the context for a chat (atomic write)."""
    turns = load_context(chat_jid)
    turns.append({"ts": time.time(), "role": role, "text": text})
    turns = turns[-CONTEXT_MAX_MESSAGES:]
    path = _context_path(chat_jid)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(turns))
    tmp.rename(path)


def format_context_block(turns: list[dict]) -> str:
    """Format context turns as a readable block for the agent prompt."""
    if not turns:
        return ""
    lines = ["Conversation context (this chat, last 30 min):"]
    for t in turns:
        speaker = "You (Tariq)" if t["role"] == "user" else "Forge"
        lines.append(f"  [{speaker}]: {t['text'][:300]}")
    return "\n".join(lines)


# ── Long-term memory (gbrain) ─────────────────────────────────────────────────
# Redact obvious secrets from any recalled snippet before it enters a prompt.
_SECRET_RX = re.compile(
    r"(sk-ant-[A-Za-z0-9_-]{6,}|[A-Za-z0-9_/+=-]{32,}|\b\d{9,}\b|"
    r"\b(password|secret|token|credential|api[_-]?key)\b\S*)", re.I)
# Raw transcripts are noisy and sometimes contain credentials — exclude from recall.
_RECALL_SKIP_PREFIXES = ("transcripts/",)


def gbrain_recall(query: str, max_chars: int = 1200, max_entries: int = 4) -> str:
    """Hybrid-search gbrain and return a high-signal recall block for the prompt.

    Only curated sources (memory/projects/design-docs/skills/…) — raw transcripts
    are skipped and secret-looking tokens are redacted. Fail-safe: any error returns
    "" so memory never blocks a dispatch. OPERATOR ONLY (callers must not pass
    delegated-role requests — would leak Tariq's broader memory into a client reply).
    """
    q = (query or "").strip()
    if not q:
        return ""
    try:
        r = subprocess.run([GBRAIN, "query", q, "--detail", "low", "--limit", "12"],
                           capture_output=True, text=True, timeout=GBRAIN_TIMEOUT)
        out = (r.stdout or "").strip()
        if r.returncode != 0 or not out:
            return ""
        # Parse "[score] slug -- snippet" entries (snippets may span lines).
        entries, cur = [], None
        for line in out.splitlines():
            m = re.match(r"^\[[0-9.]+\]\s+(\S+)\s+--\s+(.*)", line)
            if m:
                if cur:
                    entries.append(cur)
                cur = [m.group(1), m.group(2)]
            elif cur:
                cur[1] += " " + line.strip()
        if cur:
            entries.append(cur)
        kept = []
        for slug, snip in entries:
            if slug.startswith(_RECALL_SKIP_PREFIXES):
                continue
            snip = _SECRET_RX.sub("[redacted]", snip).strip()
            if snip:
                kept.append(f"- ({slug}) {snip[:240]}")
            if len(kept) >= max_entries:
                break
        if not kept:
            return ""
        return ("Relevant memory (gbrain — curated; verify before acting):\n"
                + "\n".join(kept))[:max_chars]
    except Exception as e:
        log.warning("gbrain recall failed: %s", e)
        return ""


def gbrain_persist(chat_name: str, instruction: str, response: str) -> None:
    """Persist a Forge exchange to gbrain so future sessions can recall it.

    Per-exchange page under memory/forge-chat/<date>/. Fail-safe (best effort).
    """
    if not (instruction or response):
        return
    try:
        import datetime as _dt
        now = _dt.datetime.now()
        slug = f"memory/forge-chat/{now:%Y-%m-%d}/{int(time.time())}"
        body = (
            f"---\ntype: concept\ntitle: Forge chat — {chat_name} {now:%Y-%m-%d %H:%M}\n"
            f"tags: [forge-chat]\n---\n\n"
            f"# Forge interaction — {chat_name} — {now:%Y-%m-%d %H:%M}\n\n"
            f"**Requester:** {instruction[:1000]}\n\n**Forge:** {response[:2000]}\n"
        )
        subprocess.run([GBRAIN, "put", slug], input=body, capture_output=True,
                       text=True, timeout=GBRAIN_TIMEOUT)
        log.info("gbrain: persisted exchange → %s", slug)
    except Exception as e:
        log.warning("gbrain persist failed: %s", e)


# ── /forge + @forge detection ─────────────────────────────────────────────────
def _matched_trigger(text: str) -> str | None:
    """Return the trigger ("/forge" or "@forge") that opens this message, else None."""
    t = text.strip().lower()
    for trig in FORGE_TRIGGERS:
        if t == trig or t.startswith(trig + " "):
            return trig
    return None


def is_forge_command(text: str) -> bool:
    return _matched_trigger(text) is not None


def extract_forge_instruction(text: str) -> str:
    t = text.strip()
    trig = _matched_trigger(t)
    if not trig:
        return ""
    return t[len(trig):].strip()


def check_whitelist_access(sender_jid: str, instruction: str, whitelist: dict) -> tuple[bool, str]:
    entry = whitelist.get(sender_jid)
    if not entry:
        return False, "not in whitelist"
    if entry.get("access") == "full":
        return True, "full access"
    first_word = instruction.split()[0].lower() if instruction else ""
    allowed = [c.lower() for c in entry.get("allowed_commands", [])]
    if first_word in allowed:
        return True, f"limited — '{first_word}' allowed"
    return False, f"limited — '{first_word}' not in {allowed}"


# ── NO_REPLY sentinel ─────────────────────────────────────────────────────────
def is_no_reply(text: str) -> bool:
    """True if the model's ENTIRE output is the NO_REPLY sentinel.

    Normalized: strip whitespace/quotes/punctuation, uppercase. Must be the whole
    response — 'NO_REPLY' or 'NO REPLY' alone suppress; 'Okay, NO_REPLY' does NOT
    (partial sentinel with surrounding text is still delivered, so the prompt and
    this check are kept tight together).
    """
    if not text:
        return False
    norm = re.sub(r"[\s\"'`.*_]+", "", text.strip()).upper()
    return norm in ("NOREPLY", "NO_REPLY")


# Synthesized permission set for the operator (Tariq) — full control.
OPERATOR = {"name": "Tariq", "role": "commander", "answer": "all",
            "action": "all", "reply": "auto", "scope": None, "project": None,
            "deny": [], "deny_action": []}


# ── Routing ───────────────────────────────────────────────────────────────────
def resolve_tier(msg: dict, routing_table: dict, permissions: dict,
                 whitelist: dict) -> tuple[str, str | None, dict | None]:
    """
    Returns (tier, agent_id_or_none, person_or_none).
    Tiers: "forge_cmd" | "allowlisted_dm" | "tb" | "personal" | "notify" | "skip"
    person: resolved permission set (OPERATOR for Tariq, role dict for allowlisted).
    """
    text = msg["text"]
    raw_text = msg.get("raw_text", text)
    is_from_me = msg["from_me"] == 1
    chat = msg["chat_jid"]
    is_group = chat.endswith("@g.us")

    if is_forge_command(raw_text) or is_forge_command(text):
        if is_from_me:
            return ("forge_cmd", "main", OPERATOR)
        sender = msg["sender_jid"]
        # 1) permissions.json (roles)
        person = resolve_person(sender, permissions)
        if person:
            # allowlisted invocation routes per role; in a bound group use that agent
            agent = (routing_table.get(chat) or "main") if is_group else "main"
            log.info("Permissions %s: role=%s reply=%s", sender,
                     person.get("role"), person.get("reply"))
            return ("forge_cmd", agent, person)
        # 2) legacy whitelist (full/limited) → treat as commander-equivalent
        instruction = extract_forge_instruction(text)
        allowed, reason = check_whitelist_access(sender, instruction, whitelist)
        if allowed:
            log.info("Whitelist %s: %s", sender, reason)
            return ("forge_cmd", "main", OPERATOR)
        # 3) not allowlisted: a stranger explicitly invoked Forge → notify Tariq
        log.info("Unauthorized @forge from %s — notifying operator", sender)
        return ("notify", None, None)

    if is_from_me:
        return ("skip", None, None)

    # Non-command DM from an allowlisted person → free-chat per their role.
    # Groups stay on their current path (TB pre-screen or personal triage) —
    # free-chat is DM-only so Forge doesn't respond to every group message.
    if not is_group:
        person = resolve_person(msg["sender_jid"], permissions)
        if person:
            log.info("Free-chat DM from %s (role=%s)", person.get("name"),
                     person.get("role"))
            return ("allowlisted_dm", "main", person)

    if is_group:
        agent_id = routing_table.get(chat)
        if agent_id:
            return ("tb", agent_id, None)
        return ("personal", None, None)  # unbound group → Ollama triage

    return ("personal", None, None)  # DM → Ollama triage


# ── Ollama ────────────────────────────────────────────────────────────────────
def _ollama_chat(prompt: str, model: str = OLLAMA_TRIAGE_MODEL,
                 timeout: int | None = None) -> str:
    """Call Ollama /api/chat and return the assistant message text.

    timeout defaults to OLLAMA_TIMEOUT_SECONDS (45s) for triage models.
    Pass OLLAMA_DRAFT_TIMEOUT_SECONDS (180s) for the larger draft model.
    """
    _timeout = timeout if timeout is not None else OLLAMA_TIMEOUT_SECONDS
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.1},
        # Disable thinking mode in Qwen3 for speed
        "think": False,
        # Pin the model in memory so it never reloads from disk between calls
        "keep_alive": OLLAMA_KEEP_ALIVE,
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_timeout) as resp:
            return json.load(resp)["message"]["content"]
    except Exception as e:
        log.warning("Ollama call failed (%s): %s", model, e)
        return ""


def _ollama_describe_image(image_path: str) -> str:
    """Use moondream to describe an image for the Ollama fallback path.

    Returns a plain-English description, or "" on failure.
    moondream is tiny (1.6 GB) and fast — purpose-built for image captioning.
    """
    try:
        with open(image_path, "rb") as fh:
            image_b64 = base64.b64encode(fh.read()).decode()
    except Exception as e:
        log.warning("Cannot read image %s: %s", image_path, e)
        return ""

    payload = json.dumps({
        "model": OLLAMA_VISION_MODEL,
        "messages": [{
            "role": "user",
            "content": "Describe this image in detail. Include any text, people, objects, setting, and anything Tariq might need to know.",
            "images": [image_b64],
        }],
        "stream": False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            desc = json.load(resp)["message"]["content"]
            log.info("moondream described image (%d chars): %r", len(desc), desc[:80])
            return desc
    except Exception as e:
        log.warning("Ollama vision (moondream) failed: %s", e)
        return ""


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of a string (handles leading/trailing prose)."""
    m = re.search(r'\{[^{}]*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    return {}


def _log_triage(msg: dict, result: dict, tier: str) -> None:
    import datetime
    dt = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    line = json.dumps({
        "ts": dt,
        "tier": tier,
        "chat": msg.get("chat_name") or msg.get("chat_jid"),
        "sender": msg.get("sender_name") or msg.get("sender_jid"),
        "text_preview": msg.get("text", "")[:80],
        "result": result,
    })
    with open(TRIAGE_LOG, "a") as fh:
        fh.write(line + "\n")


def ollama_prescreen_tb(msg: dict) -> bool:
    """
    Tier 2: True Build group pre-screen.
    Returns True if Claude should draft a response, False to skip.
    Falls back to True (safe default) if Ollama is unavailable.
    """
    chat = msg["chat_name"] or msg["chat_jid"]
    sender = msg["sender_name"] or msg["sender_jid"]
    text = msg["text"].strip() or f"[{msg['media_type'] or 'media'}]"

    prompt = f"""You are screening a WhatsApp message from a True Build construction project group for Tariq Ismail (the business owner).

Decide if this message requires a response from Tariq.

Group: {chat}
Sender: {sender}
Message: {text}

Reply with JSON only:
{{"needs_draft": true, "reason": "one sentence"}}
or
{{"needs_draft": false, "reason": "one sentence"}}

needs_draft TRUE if: direct question to Tariq, approval or confirmation request, client asking for project status or timeline, contractor flagging a problem, someone waiting on an answer.
needs_draft FALSE if: photo or video share with no question, general progress update, simple "noted" or thumbs-up acknowledgement, conversation between other parties that doesn't address Tariq."""

    raw = _ollama_chat(prompt)
    result = _extract_json(raw)
    needs = result.get("needs_draft", True)  # safe default: assume yes
    _log_triage(msg, result, "tb-prescreen")
    log.info(
        "TB pre-screen [%s] needs_draft=%s — %s",
        chat, needs, result.get("reason", "?")
    )
    return bool(needs)


def ollama_triage_personal(msg: dict) -> dict:
    """
    Tier 3: Personal / unbound group triage.
    Returns {"action": "notify"|"skip", "priority": "high"|"normal", "summary": str}
    Falls back to skip on Ollama failure (personal messages are lower stakes).
    """
    chat = msg["chat_name"] or msg["chat_jid"]
    is_group = msg["chat_jid"].endswith("@g.us")
    chat_type = "group" if is_group else "direct message"
    sender = msg["sender_name"] or msg["sender_jid"]
    text = msg["text"].strip() or f"[{msg['media_type'] or 'media'} — no text]"

    prompt = f"""You are screening WhatsApp messages for Tariq Ismail, a property developer and entrepreneur in Dubai.

Decide if this message needs Tariq's attention.

Chat: {chat} ({chat_type})
Sender: {sender}
Message: {text}

Reply with JSON only:
{{"action": "notify", "priority": "high", "summary": "one sentence describing what needs attention"}}
or
{{"action": "skip", "priority": "low", "summary": "one sentence why it can be ignored"}}

Use "notify" for: direct question to Tariq, message from his wife Sana or close family, urgent business outside True Build groups, time-sensitive personal matter, someone waiting on a reply.
Use "skip" for: group chatter not directed at Tariq, shared videos or memes, general updates no one asked Tariq about, casual conversation between others in a group."""

    raw = _ollama_chat(prompt)
    result = _extract_json(raw)
    if not result or "action" not in result:
        result = {"action": "skip", "priority": "low", "summary": "Ollama returned no valid JSON"}
    _log_triage(msg, result, "personal")
    log.info(
        "Personal triage [%s / %s] → %s (%s)",
        chat, sender, result.get("action"), result.get("summary", "")
    )
    return result


# ── Telegram direct send ──────────────────────────────────────────────────────
def send_telegram(text: str) -> None:
    """Send a message directly via Telegram Bot API — no openclaw needed."""
    payload = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        log.info("Telegram sent: %r", text[:80])
    except Exception as e:
        log.error("Telegram send failed: %s", e)


def format_personal_alert(msg: dict, triage: dict) -> str:
    import datetime
    chat = msg["chat_name"] or msg["chat_jid"]
    sender = msg["sender_name"] or msg["sender_jid"]
    text = msg["text"].strip()
    dt = datetime.datetime.fromtimestamp(msg["ts"], datetime.timezone.utc).strftime("%H:%M")
    priority_emoji = "🔴" if triage.get("priority") == "high" else "💬"

    lines = [f"{priority_emoji} WhatsApp — {chat}"]
    lines.append(f"From: {sender} at {dt}")
    if text:
        lines.append(f'"{text[:200]}"')
    lines.append(f"→ {triage.get('summary', '')}")
    return "\n".join(lines)


# ── Session hygiene ───────────────────────────────────────────────────────────
SESSION_RETENTION_SECONDS = 86400  # keep 24h of session transcripts for audit

def new_session_id(agent_id: str) -> str:
    """A fresh, unique session id per dispatch.

    Each dispatch runs in its own openclaw session so the agent never tries to
    RESUME a session whose .jsonl is locked by a previously-killed process —
    that resume path is what produced the recurring code=file_lock_stale errors.
    Conversation continuity is provided separately by the bridge's own injected
    context block (load_context / format_context_block), so a fresh session per
    turn loses nothing the operator sees.
    """
    return f"forge-{agent_id}-{uuid.uuid4().hex[:12]}"


def cleanup_old_sessions(agent_id: str) -> None:
    """Bound growth of the per-agent sessions dir without destroying recent data.

    Unique-session-per-dispatch means session files accumulate, so we age them
    out.  Unlike the old cleanup this runs on a long retention window and only
    removes genuinely old artifacts — it never deletes a recent/in-flight
    transcript (the previous 10-min sweep was silently deleting live history).
    """
    sessions_dir = Path.home() / ".openclaw" / "agents" / agent_id / "sessions"
    if not sessions_dir.exists():
        return
    cutoff = time.time() - SESSION_RETENTION_SECONDS
    removed = 0
    for pat in ("*.jsonl", "*.trajectory.jsonl", "*.trajectory-path.json",
                "*.jsonl.bak-*", "*.jsonl.reset.*"):
        for p in sessions_dir.glob(pat):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                pass
    if removed:
        log.info("Aged out %d old session file(s) for agent %s", removed, agent_id)


# ── Claude agent invocation ───────────────────────────────────────────────────
def _is_billing_error(stderr: str) -> bool:
    """Return True if openclaw stderr contains a billing rejection."""
    low = stderr.lower()
    return any(p.lower() in low for p in BILLING_ERROR_PATTERNS)


def _run_claude(agent_id: str, message: str, is_forge_cmd: bool,
                reply_jid: str | None = None, wacli_store: str = "",
                person: dict | None = None, forge_instruction: str = "",
                forge_chat_name: str = "",
                return_response: bool = False) -> bool | tuple[bool, str]:
    """Run openclaw agent and, for /forge commands, deliver the response.

    Returns True if a billing error was detected (caller may use Ollama fallback),
    False on success or non-billing failure.
    If return_response=True, returns (billing_failed, response_text) tuple instead.

    The agent (especially when falling back to Ollama due to billing) will
    generate a text response to stdout but NOT call wacli itself.  So for
    /forge commands the bridge extracts stdout and sends it via wacli + Telegram
    instead of relying on the agent to do it.
    """
    cleanup_old_sessions(agent_id)

    cmd = [
        "/opt/homebrew/bin/openclaw", "agent",
        "--agent", agent_id,
        "--session-id", new_session_id(agent_id),
        "--message", message,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=AGENT_TIMEOUT_SECONDS
        )
        response_text = result.stdout.strip()

        with open(AGENT_LOG, "a") as fh:
            if response_text:
                fh.write(response_text + "\n")
            if result.returncode != 0:
                fh.write(f"[exit {result.returncode}] stderr: {result.stderr[:500]}\n")

        if result.returncode != 0:
            billing = _is_billing_error(result.stderr)
            log.error("Claude agent %s exit %d%s: %s",
                      agent_id, result.returncode,
                      " (billing)" if billing else "",
                      result.stderr[:200])
            if billing:
                return (True, "") if return_response else True
            if is_forge_cmd and reply_jid:
                _deliver_forge_error(reply_jid, wacli_store)
            return (False, "") if return_response else False

        if is_forge_cmd and response_text and reply_jid:
            _deliver_by_reply_mode(reply_jid, response_text, wacli_store, person,
                                   forge_chat_name)
            gbrain_persist(forge_chat_name or reply_jid, forge_instruction, response_text)

        return (False, response_text) if return_response else False

    except subprocess.TimeoutExpired:
        log.error("Claude agent %s timed out after %ds", agent_id, AGENT_TIMEOUT_SECONDS)
        if is_forge_cmd and reply_jid:
            _deliver_forge_error(reply_jid, wacli_store)
        return (False, "") if return_response else False
    except Exception as e:
        log.error("Claude agent %s error: %s", agent_id, e)
        return (False, "") if return_response else False


# ── Ollama fallbacks (used when Claude billing is exhausted) ──────────────────
def _ollama_forge_fallback(instruction: str, reply_jid: str, chat_name: str,
                           local_path: str = "", wacli_store: str = "") -> None:
    """Handle a /forge command locally via qwen2.5:72b when Claude billing fails."""
    log.info("⚡ /forge Ollama fallback (%s) for %s", OLLAMA_DRAFT_MODEL, chat_name)

    # Inject thread context if available (reply_jid == chat_jid)
    context_turns = load_context(reply_jid)
    context_block = format_context_block(context_turns)
    context_section = f"\n{context_block}\n" if context_block else ""

    # Describe attached image via moondream if present
    image_section = ""
    if local_path and Path(local_path).exists():
        log.info("Describing attached image via moondream: %s", local_path)
        desc = _ollama_describe_image(local_path)
        if desc:
            image_section = f"\nAttached image description: {desc}\n"

    prompt = f"""You are Forge, an AI assistant for Tariq Ismail (property developer, Dubai).
Tariq has issued a direct command via WhatsApp. Execute it concisely.
{context_section}{image_section}
Command: {instruction}
Chat: {chat_name}

Reply with a short, direct response (1-4 sentences). No markdown, no preamble."""

    response = _ollama_chat(prompt, model=OLLAMA_DRAFT_MODEL,
                            timeout=OLLAMA_DRAFT_TIMEOUT_SECONDS)
    if not response:
        _deliver_forge_error(reply_jid, wacli_store)
        return

    response = response.strip()
    log.info("Ollama /forge response: %r", response[:100])
    _deliver_forge_response(reply_jid, f"{response}\n\n_(via local model — Claude credits temporarily exhausted)_", wacli_store)


def _ollama_tb_draft_fallback(msg: dict) -> None:
    """Draft a TB group reply via qwen2.5:72b when Claude billing fails.

    Writes to pending_drafts.json and sends a Telegram approval notification —
    same flow as the Claude draft path, so 'draft send [id]' still works.
    """
    import datetime as _dt
    chat_name = msg["chat_name"] or msg["chat_jid"]
    chat_jid  = msg["chat_jid"]
    sender    = msg["sender_name"] or msg["sender_jid"]
    text      = msg["text"].strip() or f"[{msg.get('media_type') or 'media'} message]"

    log.info("TB Ollama draft fallback (%s) for %s", OLLAMA_DRAFT_MODEL, chat_name)

    prompt = f"""You are drafting a professional WhatsApp reply on behalf of Tariq Ismail, owner of True Build (a construction company in Dubai).

Group: {chat_name}
Message from {sender}: {text}

Write a brief, professional response (1-3 sentences). Be direct and actionable.
Output only the message text — no explanations, no quotes, no labels."""

    draft = _ollama_chat(prompt, model=OLLAMA_DRAFT_MODEL,
                         timeout=OLLAMA_DRAFT_TIMEOUT_SECONDS)
    if not draft:
        log.error("Ollama TB draft fallback failed for %s", chat_name)
        send_telegram(
            f"⚠️ TB message needs reply but Claude billing failed and Ollama draft also failed.\n"
            f"Group: {chat_name}\nFrom: {sender}\n\"{text[:200]}\""
        )
        return

    draft = draft.strip()

    # Write to pending_drafts.json for approval via 'draft send [id]'
    drafts_file = Path.home() / "forge" / "forge-state" / "pending_drafts.json"
    try:
        drafts = json.loads(drafts_file.read_text()) if drafts_file.exists() else []
    except Exception:
        drafts = []

    draft_id = f"ollama-{int(time.time())}"
    drafts.append({
        "id": draft_id,
        "group_jid": chat_jid,
        "group_name": chat_name,
        "draft": draft,
        "source": "ollama-fallback",
        "model": OLLAMA_DRAFT_MODEL,
        "status": "pending",
        "created_at": _dt.datetime.now().isoformat(),
    })

    tmp = str(drafts_file) + ".tmp"
    Path(tmp).write_text(json.dumps(drafts, indent=2))
    Path(tmp).rename(drafts_file)

    # Telegram approval notification (same format as Claude drafts)
    send_telegram(
        f"📝 Draft ready (local model — Claude billing)\n"
        f"Group: {chat_name}\n"
        f"From: {sender}\n"
        f"Msg: {text[:200]}\n\n"
        f"Draft:\n{draft}\n\n"
        f"Reply: draft send {draft_id}"
    )
    log.info("TB Ollama draft %s written for %s", draft_id, chat_name)


def _wacli_send(chat_jid: str, message: str, wacli_store: str = "") -> subprocess.CompletedProcess:
    """Send a WhatsApp message via the correct wacli store."""
    store_args = ["--store", wacli_store] if wacli_store else []
    return subprocess.run(
        ["wacli"] + store_args + ["send", "text", "--to", chat_jid, "--message", message],
        capture_output=True, text=True,
    )


def _deliver_forge_response(chat_jid: str, text: str, wacli_store: str = "") -> None:
    """Send agent response back to the originating WhatsApp chat + Telegram."""
    msg = text[:2000]
    result = _wacli_send(chat_jid, msg, wacli_store)
    if result.returncode == 0:
        log.info("⚡ /forge response delivered to %s", chat_jid)
    else:
        log.error("wacli send failed (%s): %s", chat_jid, result.stderr[:200])
    save_to_context(chat_jid, "assistant", text[:500])
    send_telegram(f"✅ /forge:\n{msg[:500]}")


def _deliver_forge_error(chat_jid: str, wacli_store: str = "") -> None:
    """Notify Tariq when a /forge command couldn't be processed."""
    errmsg = "⚠️ /forge failed — agent didn't respond. Check logs or top up API credits at claude.ai/settings/usage"
    _wacli_send(chat_jid, errmsg, wacli_store)
    send_telegram(errmsg)


def _draft_forge_response(chat_jid: str, text: str, wacli_store: str,
                          person: dict | None, chat_name: str = "") -> None:
    """Stage a reply for one-tap Telegram approval instead of sending it directly.

    Reuses pending_drafts.json + 'draft send [id]' — the same approval flow as TB drafts.
    """
    import datetime as _dt
    drafts_file = Path.home() / "forge" / "forge-state" / "pending_drafts.json"
    try:
        drafts = json.loads(drafts_file.read_text()) if drafts_file.exists() else []
    except Exception:
        drafts = []
    draft_id = f"forge-{int(time.time())}"
    drafts.append({
        "id": draft_id,
        "group_jid": chat_jid,
        "group_name": chat_name or chat_jid,
        "draft": text[:2000],
        "source": "forge-replymode",
        "for_role": (person or {}).get("role", "?"),
        "status": "pending",
        "created_at": _dt.datetime.now().isoformat(),
    })
    tmp = str(drafts_file) + ".tmp"
    Path(tmp).write_text(json.dumps(drafts, indent=2))
    Path(tmp).rename(drafts_file)
    who = (person or {}).get("name", chat_jid)
    send_telegram(
        f"📝 Draft for {who} ({(person or {}).get('role','?')}) → {chat_name or chat_jid}\n\n"
        f"{text[:1500]}\n\nReply: draft send {draft_id}"
    )
    log.info("Drafted reply %s for %s (await approval)", draft_id, chat_jid)


def _deliver_by_reply_mode(chat_jid: str, text: str, wacli_store: str = "",
                           person: dict | None = None, chat_name: str = "") -> None:
    """Deliver a /forge response according to the requester's role reply mode.

    - NO_REPLY sentinel  → suppress entirely (principle #2).
    - reply == auto      → send directly in chat.
    - reply == draft     → stage for Telegram approval.
    - reply == notify    → Telegram alert only, nothing sent to the chat.
    SAFETY: any outbound to a GROUP from a non-commander is forced to draft until
    the hard send-gates land (principle #4 — no autonomous group posts).
    """
    if is_no_reply(text):
        log.info("NO_REPLY — suppressing delivery to %s", chat_jid)
        return

    p = person or OPERATOR
    role = p.get("role", "commander")
    reply_mode = p.get("reply", "auto")
    is_group = chat_jid.endswith("@g.us")
    if is_group and role != "commander":
        reply_mode = "draft"

    if reply_mode == "auto":
        _deliver_forge_response(chat_jid, text, wacli_store)
    elif reply_mode == "draft":
        _draft_forge_response(chat_jid, text, wacli_store, p, chat_name)
    else:  # notify
        send_telegram(f"💬 Forge (notify-only · {role}) for {chat_name or chat_jid}:\n{text[:800]}")


def dispatch_claude(agent_id: str, message: str, is_forge_cmd: bool = False,
                    reply_jid: str | None = None,
                    forge_instruction: str | None = None,
                    forge_chat_name: str | None = None,
                    forge_local_path: str = "",
                    wacli_store: str = "",
                    person: dict | None = None) -> None:
    """Submit a Claude agent run to the thread pool.

    wacli_store — the wacli store dir for the originating account
    (e.g. ~/.wacli for personal, ~/.wacli-business for business).
    All outbound wacli sends use this so replies go back on the right account.
    person — resolved permission set; controls reply mode (auto/draft/notify).
    """
    tag = "⚡ /forge →" if is_forge_cmd else "→ claude"
    log.info("%s agent=%s account=%s preview=%r", tag, agent_id,
             "business" if "business" in wacli_store else "personal", message[:80])

    if is_forge_cmd and reply_jid is not None and forge_instruction is not None:
        _agent_id = agent_id
        _message  = message
        _reply    = reply_jid
        _instr    = forge_instruction
        _name     = forge_chat_name or ""
        _path     = forge_local_path
        _store    = wacli_store
        _person   = person

        def _forge_task() -> None:
            billing_failed = _run_claude(_agent_id, _message, True, _reply, _store,
                                         _person, _instr, _name)
            if billing_failed:
                # Ollama fallback only makes sense for auto-reply (operator); for
                # draft/notify roles a billing failure just notifies, no local draft.
                if (_person or OPERATOR).get("reply", "auto") == "auto":
                    log.warning("Billing error on /forge — falling back to %s", OLLAMA_DRAFT_MODEL)
                    _ollama_forge_fallback(_instr, _reply, _name, _path, _store)
                else:
                    send_telegram(f"⚠️ /forge from {_name}: Claude unavailable; no reply sent.")

        _claude_pool.submit(_forge_task)
    else:
        _claude_pool.submit(_run_claude, agent_id, message, is_forge_cmd, reply_jid, wacli_store)


# ── Tier handlers (run inside Ollama thread pool) ─────────────────────────────
def _handle_tb(msg: dict, agent_id: str, wacli_store: str = "") -> None:
    """Tier 2: Ollama pre-screen → Claude → write draft to pending_drafts.json."""
    if not ollama_prescreen_tb(msg):
        return
    formatted = format_for_claude(msg, None, wacli_store)
    future = _claude_pool.submit(_run_claude, agent_id, formatted, False, None, wacli_store,
                                  return_response=True)
    try:
        result = future.result(timeout=AGENT_TIMEOUT_SECONDS + 30)
        billing_failed, response_text = result
        if billing_failed:
            log.warning("Billing error on TB draft — falling back to %s", OLLAMA_DRAFT_MODEL)
            _ollama_tb_draft_fallback(msg)
            return
        if response_text:
            _write_tb_draft(msg, response_text, wacli_store)
    except Exception as e:
        log.error("TB Claude agent error: %s — falling back to Ollama draft", e)
        _ollama_tb_draft_fallback(msg)


def _write_tb_draft(msg: dict, draft_text: str, wacli_store: str = "") -> None:
    """Write a TB draft to pending_drafts.json and notify via Telegram."""
    import datetime as _dt
    drafts_file = Path.home() / "forge" / "forge-state" / "pending_drafts.json"
    try:
        drafts = json.loads(drafts_file.read_text()) if drafts_file.exists() else []
    except Exception:
        drafts = []
    chat_jid = msg["chat_jid"]
    chat_name = msg.get("chat_name") or chat_jid
    sender = msg.get("sender_name") or msg.get("sender_jid") or "Unknown"
    draft_id = f"tb-{int(time.time())}"
    drafts.append({
        "id": draft_id,
        "group_jid": chat_jid,
        "group_name": chat_name,
        "draft": draft_text[:2000],
        "source": "tb-ops",
        "for_role": "tb-group",
        "sender": sender,
        "trigger_text": (msg.get("text") or "")[:200],
        "status": "pending",
        "created_at": _dt.datetime.now().isoformat(),
        "wacli_store": wacli_store,
    })
    tmp = str(drafts_file) + ".tmp"
    Path(tmp).write_text(json.dumps(drafts, indent=2))
    Path(tmp).rename(drafts_file)
    send_telegram(
        f"✏️ DRAFT — {chat_name}\n"
        f"Re: {sender}\n\n"
        f"{draft_text[:1500]}\n\n"
        f"Approve in Forge Admin or reply: draft send {draft_id}"
    )
    log.info("TB draft %s written for %s (pending approval)", draft_id, chat_name)


def _handle_personal(msg: dict) -> None:
    """Tier 3: Ollama triage → maybe Telegram."""
    result = ollama_triage_personal(msg)
    if result.get("action") == "notify":
        send_telegram(format_personal_alert(msg, result))


# ── Message formatting for Claude ─────────────────────────────────────────────
def format_for_claude(msg: dict, person: dict | None, wacli_store: str = "") -> str:
    import datetime
    chat = msg["chat_jid"]
    is_group = chat.endswith("@g.us")
    is_from_me = msg["from_me"] == 1
    text = msg["text"].strip()
    is_cmd = is_forge_command(text)

    if is_cmd:
        instruction = extract_forge_instruction(text)
        p = person or OPERATOR
        role = p.get("role", "commander")
        sender_label = "Tariq (owner)" if is_from_me else (
            msg["sender_name"] or msg["sender_jid"]
        )

        # Hard, deterministic policy header — set by the bridge from the sender's
        # trust/role BEFORE the agent sees the request (principle #1). The agent
        # does not get to decide who it serves or how it may reply.
        if role == "commander":
            lines = ["[FORGE COMMAND — OPERATOR — DIRECT ACTION AUTHORIZED]"]
            lines.append(f"Issued by: {sender_label}")
        else:
            lines = [f"[FORGE COMMAND — DELEGATED ACCESS — role: {role}]"]
            lines.append(f"Issued by: {sender_label} (name: {p.get('name', '?')})")
            lines.append(f"This role MAY ANSWER: {p.get('answer')}")
            denied = (p.get("deny_action") or []) + (p.get("deny") or [])
            lines.append(f"This role MAY ACTION: {p.get('action')}"
                         + (f" | DENIED: {denied}" if denied else ""))
            if p.get("scope") == "own_project" or p.get("project"):
                lines.append(f"SCOPE LIMIT: only their own project "
                             f"({p.get('project') or 'their project'}). "
                             "Never reveal other clients/projects/financials.")
            lines.append(f"Reply mode: {p.get('reply', 'draft')} — the bridge handles "
                         "delivery/approval; you only produce the reply text.")

        context_turns = load_context(chat)
        context_block = format_context_block(context_turns)
        if context_block:
            lines += ["", context_block, ""]
        # Long-term memory recall — OPERATOR ONLY (never delegated roles).
        if role == "commander" and instruction:
            recall = gbrain_recall(instruction)
            if recall:
                lines += ["", recall, ""]
        lines.append(f"Instruction: {instruction or '(bare trigger — ask what they need)'}")
        # Attach image path if media was included with the command
        local_path = msg.get("local_path", "")
        if local_path and Path(local_path).exists():
            lines.append(f"Attached image: {local_path}")
            lines.append("(Use Read tool on this path to view the image)")
        lines.append(f"Context: {msg['chat_name'] or chat} ({'group' if is_group else 'DM'})")
        lines.append(f"Chat JID: {chat}")

        if role == "commander":
            wacli_cmd = f"wacli --store {wacli_store} send text" if wacli_store else "wacli send text"
            lines += [
                "",
                "Instructions for Forge:",
                "1. Execute immediately. Do not draft-and-wait.",
                "2. To REPLY in THIS chat, just output the reply text — the bridge delivers it.",
                f"3. To post to a DIFFERENT chat/group, use `{wacli_cmd} --to [JID] --message \"...\"`.",
                "4. If no reply is warranted, output exactly: NO_REPLY",
            ]
        else:
            lines += [
                "",
                "Instructions for Forge:",
                "1. Output ONLY the reply text for this person. Do NOT call wacli or telegram "
                "yourself — the bridge delivers per the reply mode above.",
                "2. Stay strictly within this role's permitted answer topics and scope.",
                "3. If the request is outside scope, or no reply is warranted, output exactly: NO_REPLY",
            ]
    elif person:
        # Allowlisted free-chat: a non-command DM from a person with a role.
        p = person
        role = p.get("role", "?")
        sender_label = msg["sender_name"] or msg["sender_jid"]
        lines = [f"[INBOUND DM — ALLOWLISTED — role: {role}]"]
        lines.append(f"From: {sender_label} (name: {p.get('name', '?')})")
        lines.append(f"This role MAY ANSWER: {p.get('answer')}")
        denied = (p.get("deny_action") or []) + (p.get("deny") or [])
        lines.append(f"This role MAY ACTION: {p.get('action')}"
                     + (f" | DENIED: {denied}" if denied else ""))
        if p.get("scope") == "own_project" or p.get("project"):
            lines.append(f"SCOPE LIMIT: only their own project "
                         f"({p.get('project') or 'their project'}). "
                         "Never reveal other clients/projects/financials.")
        lines.append(f"Reply mode: {p.get('reply', 'draft')} — the bridge handles delivery.")
        context_turns = load_context(chat)
        context_block = format_context_block(context_turns)
        if context_block:
            lines += ["", context_block, ""]
        if text:
            lines.append(f"Message: {text}")
        media = msg["media_type"]
        if media:
            fn = msg["filename"]
            lines.append(f"Media: {media}" + (f" ({fn})" if fn else ""))
            if msg["media_caption"]:
                lines.append(f"Caption: {msg['media_caption'].strip()}")
        lines.append(f"Chat JID: {chat}")
        lines += [
            "",
            "Instructions for Forge:",
            "1. If this message is within this role's scope, produce a helpful reply.",
            "2. Output ONLY the reply text — do NOT call wacli or telegram.",
            "3. If outside scope, trivial (e.g. 'ok thanks'), or no reply warranted: NO_REPLY",
        ]
    else:
        lines = ["[INBOUND WHATSAPP MESSAGE]"]
        if is_group:
            lines.append(f"Group: {msg['chat_name'] or chat}")
            lines.append(f"Group JID: {chat}")
            lines.append(f"From: {msg['sender_name'] or msg['sender_jid']} ({msg['sender_jid']})")
        else:
            lines.append(f"From: {msg['sender_name'] or msg['sender_jid']} ({msg['sender_jid']})")
        if text:
            lines.append(f"Message: {text}")
        media = msg["media_type"]
        if media:
            fn = msg["filename"]
            lines.append(f"Media: {media}" + (f" ({fn})" if fn else ""))
            if msg["media_caption"]:
                lines.append(f"Caption: {msg['media_caption'].strip()}")
            local_path = msg.get("local_path", "")
            if local_path and Path(local_path).exists():
                lines.append(f"Saved at: {local_path}")
                if media == "image":
                    lines.append("(Use Read tool on this path to view the image before responding)")

    dt = datetime.datetime.fromtimestamp(msg["ts"], datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"Time: {dt}")
    lines.append(f"Message ID: {msg['msg_id']}")
    return "\n".join(lines)


# ── Sync health monitoring ────────────────────────────────────────────────────
def check_db_health(label: str, db_path: Path) -> None:
    """Alert via Telegram if the wacli sync launchd service has no PID (7am–11pm).

    Checks the actual service state via launchctl — much more reliable than DB
    mtime, which only updates when messages arrive (quiet periods ≠ disconnected).
    Clears automatically once the service is running again.
    """
    import datetime
    import re
    if not (7 <= datetime.datetime.now().hour < 23):
        return

    service = f"ai.forge.wa-sync-{label}" if label == "business" else "ai.forge.wa-sync"
    alerted_key = f"down_alerted_{label}"

    try:
        result = subprocess.run(
            ["launchctl", "list", service],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            # Service not loaded at all
            if not _health_state.get(alerted_key):
                _health_state[alerted_key] = True
                send_telegram(
                    f"⚠️ WhatsApp {label} sync service not loaded\n"
                    f"Run: launchctl load ~/Library/LaunchAgents/{service}.plist"
                )
                log.warning("[%s] sync service not found in launchctl", label)
            return

        pid_match  = re.search(r'"PID"\s*=\s*(\d+)', result.stdout)
        exit_match = re.search(r'"LastExitStatus"\s*=\s*(-?\d+)', result.stdout)
        has_pid    = bool(pid_match)
        last_exit  = int(exit_match.group(1)) if exit_match else 0

        if not has_pid:
            if not _health_state.get(alerted_key):
                _health_state[alerted_key] = True
                exit_note = f" (last exit: {last_exit})" if last_exit not in (0, -15) else ""
                send_telegram(
                    f"⚠️ WhatsApp {label} sync is down{exit_note}\n"
                    f"launchd will restart it. Check: launchctl list | grep ai.forge.wa-sync"
                )
                log.warning("[%s] sync has no PID — not running (last exit %d)", label, last_exit)
        else:
            _health_state[alerted_key] = False   # clear once running again

    except Exception as e:
        log.warning("Health check for %s failed: %s", label, e)


def _account_latest_age_min(db_path: Path) -> float | None:
    """Minutes since the newest message in this account's DB, or None if unknown."""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute("SELECT MAX(ts) FROM messages").fetchone()
        conn.close()
        if not row or not row[0]:
            return None
        return (time.time() - float(row[0])) / 60.0
    except Exception:
        return None


def check_sync_freshness(accounts: list) -> None:
    """Detect a 'zombie' sync: process alive but not receiving messages.

    The 2026-05-23 failure: the business sync stayed connected but delivered 0
    messages for hours while its PID stayed up, so the PID-based check missed it.
    Low-false-positive heuristic: only flag a stale account when SOME OTHER account
    proves the system + network are actively receiving (so it's account-specific,
    not a quiet period or a machine asleep). Deduped; clears on recovery.
    """
    import datetime
    if not (7 <= datetime.datetime.now().hour < 23):
        return
    ages = {a["label"]: _account_latest_age_min(a["db"]) for a in accounts}
    known = [v for v in ages.values() if v is not None]
    if not known:
        return
    system_fresh = min(known) <= SYNC_SYSTEM_FRESH_MIN
    for label, age in ages.items():
        key = f"zombie_alerted_{label}"
        if age is not None and age >= SYNC_STALE_ALERT_MIN and system_fresh:
            if not _health_state.get(key):
                _health_state[key] = True
                store_sfx = "-business" if label == "business" else ""
                send_telegram(
                    f"🧟 WhatsApp {label} sync may be STUCK — newest message is "
                    f"{age/60:.1f}h old while another account is live. Likely connected "
                    f"but not receiving (a re-link may be needed).\n"
                    f"Check: wacli --read-only --store ~/.wacli{store_sfx} doctor"
                )
                log.warning("[%s] sync freshness alert: %.0f min stale while system fresh",
                            label, age)
        elif age is not None and age < SYNC_STALE_ALERT_MIN:
            _health_state[key] = False  # recovered


# ── Main loop ─────────────────────────────────────────────────────────────────
def _poll_account(account: dict, routing_table: dict, permissions: dict,
                  whitelist: dict, stale_skipped_ref: list) -> None:
    """Poll one WhatsApp account DB and dispatch messages."""
    label      = account["label"]
    db_path    = account["db"]
    store      = account["store"]
    cursor_file = account["cursor_file"]

    if not db_path.exists():
        log.debug("DB not found for %s account — wacli sync not running?", label)
        return

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        last_rowid = load_cursor(cursor_file)
        if last_rowid == -1:
            last_rowid = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM messages").fetchone()[0]
            save_cursor(last_rowid, cursor_file)
            log.info("[%s] First run: watermark set to rowid=%d", label, last_rowid)
            conn.close()
            return
        messages = fetch_new_messages(conn, last_rowid)
        conn.close()
    except sqlite3.OperationalError as e:
        if "no such file" in str(e) or "unable to open" in str(e):
            log.warning("[%s] wacli.db not accessible — sync not running?", label)
        else:
            log.error("[%s] SQLite error: %s", label, e)
        return
    except Exception as e:
        log.error("[%s] Poll error: %s", label, e)
        return

    dispatched = 0
    for msg in messages:
        last_rowid = msg["rowid"]
        try:
            tier, agent_id, person = resolve_tier(msg, routing_table, permissions, whitelist)

            if tier == "skip":
                continue

            if tier == "notify":
                # A non-allowlisted contact explicitly invoked Forge (@forge/​/forge).
                # Forge stays silent in the chat and just alerts Tariq.
                sender = msg.get("sender_name") or msg.get("sender_jid")
                chat   = msg.get("chat_name") or msg.get("chat_jid")
                send_telegram(
                    f"🔔 Unauthorized Forge request\nFrom: {sender}\nChat: {chat}\n"
                    f"\"{msg['text'][:200]}\"\n(not allowlisted — no action taken)"
                )
                dispatched += 1
                continue

            # Per-tier age gate: TB and /forge commands survive 4 h so they are
            # never dropped after a reboot or extended wacli reconnect.  Personal /
            # unbound messages stay at 30 min to prevent Ollama overload on backfill.
            age_limit = (MAX_TB_MESSAGE_AGE_SECONDS
                         if tier in ("tb", "forge_cmd")
                         else MAX_MESSAGE_AGE_SECONDS)
            msg_age = time.time() - msg["ts"]
            if msg_age > age_limit:
                stale_skipped_ref[0] += 1
                if tier == "tb":
                    log.warning(
                        "[%s] Dropping TB msg from %s (%.0f min old > %dh limit)",
                        label, msg.get("chat_name") or msg.get("chat_jid"),
                        msg_age / 60, age_limit // 3600,
                    )
                elif stale_skipped_ref[0] % 50 == 1:
                    log.info("[%s] Skipping stale msgs (%.0f min old, %d so far)",
                             label, msg_age / 60, stale_skipped_ref[0])
                continue

            elif tier == "forge_cmd":
                _cmd_text = msg.get("raw_text", msg["text"])
                instruction  = extract_forge_instruction(_cmd_text)
                chat_jid     = msg["chat_jid"]
                chat_name    = msg["chat_name"] or chat_jid
                local_path   = msg.get("local_path", "") or ""
                sender_label = "Tariq" if msg["from_me"] == 1 else (msg["sender_name"] or msg["sender_jid"])
                save_to_context(chat_jid, "user", instruction or "(bare /forge)")
                media_note = f" [+{msg['media_type']}]" if msg.get("media_type") else ""
                acct_note  = " [business]" if label == "business" else ""
                role_note = f" ({person['role']})" if person and person.get("role") != "commander" else ""
                send_telegram(f"⚡ /forge{acct_note} from {chat_name} [{sender_label}{role_note}]{media_note}:\n{instruction or '(bare /forge)'}\n— Processing…")
                dispatch_claude(
                    agent_id,
                    format_for_claude(msg, person, store),
                    is_forge_cmd=True,
                    reply_jid=chat_jid,
                    forge_instruction=instruction,
                    forge_chat_name=chat_name,
                    forge_local_path=local_path,
                    wacli_store=store,
                    person=person,
                )
                dispatched += 1

            elif tier == "allowlisted_dm":
                chat_jid    = msg["chat_jid"]
                chat_name   = msg["chat_name"] or chat_jid
                text_preview = (msg["text"].strip() or "[media]")[:80]
                save_to_context(chat_jid, "user", msg["text"].strip()[:300])
                role_label = person.get("role", "?") if person else "?"
                send_telegram(f"💬 Free-chat from {person.get('name', chat_jid)} "
                              f"({role_label}):\n\"{text_preview}\"\n— Processing…")
                dispatch_claude(
                    "main",
                    format_for_claude(msg, person, store),
                    is_forge_cmd=True,  # reuse forge delivery (reply mode + persist)
                    reply_jid=chat_jid,
                    forge_instruction=msg["text"].strip()[:300],
                    forge_chat_name=chat_name,
                    wacli_store=store,
                    person=person,
                )
                dispatched += 1

            elif tier == "tb":
                log.info("[%s] TB pre-screen queued [%s] → %s",
                         label, msg["chat_name"] or msg["chat_jid"], agent_id)
                _ollama_pool.submit(_handle_tb, msg, agent_id, store)
                dispatched += 1

            elif tier == "personal":
                log.info("[%s] Personal triage queued [%s / %s]",
                         label, msg["chat_name"] or msg["chat_jid"],
                         msg["sender_name"] or msg["sender_jid"])
                _ollama_pool.submit(_handle_personal, msg)
                dispatched += 1

        except Exception as e:
            log.error("[%s] Error processing rowid=%s: %s", label, msg.get("rowid"), e)

    if messages:
        save_cursor(last_rowid, cursor_file)
        if dispatched:
            log.info("[%s] Processed %d msg(s), dispatched %d, cursor=%d",
                     label, len(messages), dispatched, last_rowid)


def main() -> None:
    Path(ACCOUNTS[0]["cursor_file"]).parent.mkdir(parents=True, exist_ok=True)

    routing_table = load_routing_table()
    whitelist = load_whitelist()
    permissions = load_permissions()
    log.info(
        "Started — accounts=%s | %d TB bindings | %d commanders | "
        "%d roles, %d allowlisted people | "
        "claude=%d ollama=%d | tb_age=%dh personal_age=%dm | reload=%ds",
        [a["label"] for a in ACCOUNTS],
        len(routing_table), len(whitelist),
        len(permissions.get("roles", {})), len(permissions.get("people", {})),
        MAX_CONCURRENT_CLAUDE, MAX_CONCURRENT_OLLAMA,
        MAX_TB_MESSAGE_AGE_SECONDS // 3600, MAX_MESSAGE_AGE_SECONDS // 60,
        int(ROUTING_RELOAD_INTERVAL),
    )

    try:
        urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=5)
        log.info("Ollama reachable at %s — model: %s", OLLAMA_URL, OLLAMA_TRIAGE_MODEL)
        # Warm-up: load qwen3.6 into memory now so the first real triage call is fast.
        # keep_alive=24h pins it resident thereafter (no swap-thrash with the 72B model).
        try:
            warm = json.dumps({
                "model": OLLAMA_TRIAGE_MODEL,
                "messages": [{"role": "user", "content": "ok"}],
                "stream": False,
                "think": False,
                "keep_alive": OLLAMA_KEEP_ALIVE,
            }).encode()
            req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=warm,
                                         headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=180)
            log.info("Ollama warm-up complete — %s resident (keep_alive=%s)",
                     OLLAMA_TRIAGE_MODEL, OLLAMA_KEEP_ALIVE)
        except Exception as e:
            log.warning("Ollama warm-up failed (will load on first use): %s", e)
    except Exception as e:
        log.warning("Ollama not reachable: %s — personal triage will skip (safe default)", e)

    stale_skipped_ref = [0]  # mutable counter shared across account polls
    routing_last_reload = time.time()   # skip reload on the very first iteration
    health_last_check   = time.time()

    reload_sentinel = Path.home() / "forge" / "forge-state" / ".permissions-reload"

    while True:
        now = time.time()

        # Sentinel file from forge-admin: immediate permissions reload
        if reload_sentinel.exists():
            try:
                reload_sentinel.unlink()
                new_permissions = load_permissions()
                if len(new_permissions.get("people", {})) != len(permissions.get("people", {})):
                    log.info("Permissions reloaded (sentinel): %d people (was %d)",
                             len(new_permissions.get("people", {})), len(permissions.get("people", {})))
                permissions = new_permissions
            except Exception as e:
                log.warning("Sentinel reload failed: %s", e)

        # Hot-reload routing table every 5 min — picks up new openclaw.json bindings
        # without requiring a bridge restart.
        if now - routing_last_reload >= ROUTING_RELOAD_INTERVAL:
            try:
                new_table       = load_routing_table()
                new_whitelist   = load_whitelist()
                new_permissions = load_permissions()
                if new_table != routing_table:
                    log.info("Routing table reloaded: %d bindings (was %d)",
                             len(new_table), len(routing_table))
                if len(new_permissions.get("people", {})) != len(permissions.get("people", {})):
                    log.info("Permissions reloaded: %d people (was %d)",
                             len(new_permissions.get("people", {})), len(permissions.get("people", {})))
                routing_table       = new_table
                whitelist           = new_whitelist
                permissions         = new_permissions
                routing_last_reload = now
            except Exception as e:
                log.warning("Routing table reload failed: %s", e)

        # DB health check — alert Telegram if a wacli sync process goes stale
        if now - health_last_check >= HEALTH_CHECK_INTERVAL:
            for account in ACCOUNTS:
                check_db_health(account["label"], account["db"])
            check_sync_freshness(ACCOUNTS)   # catch zombie syncs (alive but not receiving)
            health_last_check = now

        for account in ACCOUNTS:
            _poll_account(account, routing_table, permissions, whitelist, stale_skipped_ref)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
