# OpenClaw bridge — reference implementation

A single-file Node server (no dependencies, Node 18+) that connects the Voice PE
Realtime add-on to [OpenClaw](https://openclaw.ai). It implements the full
[agent contract](../../docs/agent-integration.md):

| Request body | What happens | Typical latency |
|---|---|---|
| `{"recall": "grandma phone"}` | Greps OpenClaw's memory markdown, returns matching lines | ~50 ms |
| `{"question": "...", "room": "kitchen"}` | One OpenClaw agent turn; answer returned, or announced later if slow | seconds–minutes |

Long turns are never killed: past `ASK_TIMEOUT_MS` the voice side hears
"still working on that", the turn finishes in the background, and the bridge
POSTs the answer to the asking room's announce endpoint itself.

## Setup

Run this on the machine where OpenClaw's gateway runs. Best practice: run it
straight from a clone of this repo — updating the bridge is then just
`git pull` + restart the service. The secret files below are `.gitignore`d, so
they survive pulls and can never be committed.

```bash
git clone https://github.com/TristanBrotherton/voicepe-realtime.git
cd voicepe-realtime/examples/openclaw-bridge

# 1. Secrets — the URL path is the lock on the ask endpoint
echo "/ask-$(openssl rand -hex 12)" > .ask-path
openssl rand -hex 24 > .announce-token   # same value goes in the add-on config
chmod 600 .ask-path .announce-token

# 2. Run (or install as a service — samples below)
ANNOUNCE_HOST=192.168.1.50 ANNOUNCE_MAP="kitchen=8090,workshop=8091" node bridge.mjs
```

Then in the **add-on configuration** (each instance):

- `openclaw_url`: `http://<bridge-machine-ip>:3338` + the contents of `.ask-path`
- `announce_port`: the port you mapped for that room (e.g. `8090`)
- `announce_token`: the contents of `.announce-token`
- `instance_name`: the room name used in `ANNOUNCE_MAP` (e.g. `kitchen`)

### Environment reference

| Variable | Default | Purpose |
|---|---|---|
| `ASK_PORT` | `3338` | Listen port |
| `ASK_PATH` | *(from `.ask-path`)* | Secret URL path — **required** |
| `OPENCLAW_BIN` | `openclaw` | OpenClaw CLI |
| `OPENCLAW_AGENT` | `main` | Agent id for voice turns |
| `OPENCLAW_WORKSPACE` | `~/.openclaw/workspace` | Where memory markdown lives |
| `ASK_TIMEOUT_MS` | `120000` | Sync window before "still working" (keep < the add-on's 145 s) |
| `ANNOUNCE_HOST` | *(unset)* | Home Assistant host — required for report-back |
| `ANNOUNCE_MAP` | *(unset)* | `room=port` pairs matching each instance's `announce_port` |
| `ANNOUNCE_TOKEN` | *(from `.announce-token`)* | Bearer token for `/announce` |

## Teach OpenClaw the announce endpoint

Add a note to OpenClaw's workspace `TOOLS.md` so its own long tasks and
scheduled jobs can speak in the house:

```markdown
## Voice PE announcements (speak in the house)
POST a short message and it is spoken aloud in that room:

    curl -s -X POST http://<ha-host>:8090/announce \
      -H "Authorization: Bearer $(cat <path-to>/.announce-token)" \
      -H "Content-Type: application/json" \
      -d '{"message":"The research is done - details in your messages."}'

- Rooms: kitchen = 8090, workshop = 8091. Voice requests tell you which room
  asked — ALWAYS announce to that room.
- ALWAYS use exec/shell curl, not a web-fetch tool (those often refuse LAN hosts).
- Write announcements in FIRST PERSON as the house voice assistant — to the
  household, you and it are the same assistant. Keep it short; it is read aloud.
- 503 = no device connected in that room; fall back to messaging.
```

## Run as a service

**macOS (launchd)** — `~/Library/LaunchAgents/openclaw.voicepe-bridge.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>openclaw.voicepe-bridge</string>
  <key>ProgramArguments</key>
  <array><string>/usr/local/bin/node</string><string>/path/to/bridge.mjs</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>ANNOUNCE_HOST</key><string>192.168.1.50</string>
    <key>ANNOUNCE_MAP</key><string>kitchen=8090,workshop=8091</string>
  </dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/openclaw.voicepe-bridge.plist
```

**Linux (systemd)** — `/etc/systemd/system/voicepe-bridge.service`:

```ini
[Unit]
Description=Voice PE Realtime - OpenClaw bridge
After=network-online.target

[Service]
ExecStart=/usr/bin/node /path/to/bridge.mjs
Environment=ANNOUNCE_HOST=192.168.1.50
Environment=ANNOUNCE_MAP=kitchen=8090,workshop=8091
Restart=always
User=youruser

[Install]
WantedBy=multi-user.target
```

## Smoke test

```bash
ASK=$(cat .ask-path)
# recall (instant, no agent turn)
curl -s -X POST "http://127.0.0.1:3338$ASK" -H "Content-Type: application/json" \
  -d '{"recall":"test"}'
# full agent turn
curl -s -X POST "http://127.0.0.1:3338$ASK" -H "Content-Type: application/json" \
  -d '{"question":"Reply with exactly: bridge OK","room":"kitchen"}'
```

## Updating

```bash
cd voicepe-realtime && git pull
# macOS
launchctl kickstart -k gui/$(id -u)/openclaw.voicepe-bridge
# Linux
sudo systemctl restart voicepe-bridge
```

Secrets and logs are untouched by updates; check the repo changelog for
contract changes when you also update the add-on.

## Security notes

- The ask endpoint's only lock is the secret path — keep the bridge on your LAN
  (do not port-forward it) and treat `.ask-path` like a password.
- The announce token lets anyone who has it speak through your devices; scope it
  to your LAN the same way.
- The bridge runs agent turns with whatever powers your OpenClaw agent has.
  Voice input is inherently open to anyone in the room — configure the agent
  (and the add-on's `male_only_tools` speaker gate, if you use it) accordingly.
