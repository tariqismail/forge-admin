# Forge Admin

A WhatsApp command centre for [openclaw](https://github.com/openclaw/openclaw). Turns openclaw from "AI chatbot on WhatsApp" into a managed assistant with role-based access, draft-approve safety, long-term memory, and a web dashboard.

## What you get

- **WhatsApp bridge** (`bin/wa-bridge.py`) -- polls [wacli](https://wacli.sh) SQLite DBs, routes messages through trust tiers, dispatches to openclaw agents
- **Role-based permissions** -- commander / staff / client / vendor / personal roles with per-person overrides; each role defines what the assistant may answer, action, and how it replies (auto / draft-for-approval / notify-only)
- **`/forge` and `@forge` triggers** -- natural command + mention invocation from WhatsApp
- **Free-chat** -- allowlisted people's normal DMs handled per their role (no trigger needed)
- **Draft-approve safety** -- non-commander group replies always staged for Telegram one-tap approval; NO_REPLY sentinel suppresses unnecessary sends
- **Long-term memory** -- gbrain recall injection (curated, secrets-redacted) + automatic exchange persistence
- **Web dashboard** (`app.py`) -- Flask + htmx admin panel for managing people, reviewing drafts, monitoring activity
- **Reliability** -- Ollama keep-alive (no model swap-thrash), zombie-sync detection, session hygiene, graceful billing fallback

## Requirements

- [openclaw](https://github.com/openclaw/openclaw) installed and configured
- [wacli](https://wacli.sh) (`brew install wacli` or from source) -- WhatsApp CLI
- Python 3.9+
- [Ollama](https://ollama.ai) with a local model (default: `qwen3.6:latest`)
- A Telegram bot (for notifications + draft approvals)
- macOS with launchd (Linux systemd templates coming)
- Optional: [gbrain](https://github.com/nichochar/gbrain) for long-term memory

## Quick start

```bash
# 1. Clone
git clone https://github.com/tariqismail/forge-admin.git ~/forge
cd ~/forge

# 2. Authenticate wacli (scan QR from your phone)
wacli auth
# For a second account (e.g. business):
wacli auth --store ~/.wacli-business

# 3. Set up secrets
cp config/.secrets.example ~/.secrets
# Edit ~/.secrets: add your Telegram bot token + chat ID
# (Create a bot via @BotFather; get your chat ID via @userinfobot)

# 4. Set up permissions
cp config/permissions.example.json forge-rules/permissions.json
mkdir -p forge-rules forge-state
# Edit forge-rules/permissions.json: add your people

# 5. Configure openclaw
# Ensure your openclaw.json has:
#   - agentRuntime: {id: "claude-cli"} on each model under agents.defaults.models
#   - The claude binary logged in (claude --version && claude -p "ok")

# 6. Pull an Ollama model
ollama pull qwen3.6:latest

# 7. Start the bridge
python3 bin/wa-bridge.py
# (Or install the launchd plist for auto-start -- see docs/SETUP.md)

# 8. Test: send "/forge hello" from your WhatsApp
```

## Web dashboard

```bash
# Install dependencies
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run in development
python3 app.py

# Production (gevent + gunicorn)
# See launchd/ai.forge.admin.plist
```

## Configuration

The bridge reads constants from the top of `bin/wa-bridge.py`. Key settings:

| Constant | Default | What it does |
|---|---|---|
| `ACCOUNTS` | personal + business wacli stores | WhatsApp accounts to poll |
| `OLLAMA_TRIAGE_MODEL` | `qwen3.6:latest` | Local model for message triage |
| `OLLAMA_KEEP_ALIVE` | `24h` | Keep the triage model resident in memory |
| `AGENT_TIMEOUT_SECONDS` | `240` | Max time for a Claude agent run |
| `FORGE_TRIGGERS` | `/forge`, `@forge` | Trigger words for direct commands |
| `GBRAIN` | `~/.bun/bin/gbrain` | Path to gbrain binary (optional) |

Telegram credentials are loaded from `~/.secrets` (see `config/.secrets.example`).

Permissions are in `forge-rules/permissions.json` (see `config/permissions.example.json` and the [permissions guide](docs/PERMISSIONS.md)).

## Architecture

```
WhatsApp (wacli) --> wa-bridge.py --> openclaw agents (Claude)
                         |                    |
                         |              Telegram (notifications + drafts)
                         |
                    Ollama (triage)     gbrain (memory)
                         |
                    Web dashboard (Flask)
```

The bridge is the routing layer. It classifies every inbound message by sender + trust tier, injects a deterministic policy header into the agent prompt (the agent doesn't decide who it serves), and enforces reply modes at delivery time. See [ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full reference.

## Roles

| Role | May answer | May action | Reply mode |
|---|---|---|---|
| **commander** | everything | everything | auto (direct reply) |
| **staff** | TB status, schedule, general | notes, reminders, action items | auto |
| **client** | own project status only | nothing | draft (approval required) |
| **vendor** | PO/payment/delivery status | nothing | draft |
| **personal** | general, schedule | reminders | draft |

See `config/permissions.example.json` for the full schema with per-person overrides.

## License

MIT
