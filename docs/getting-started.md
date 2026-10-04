# Getting Started

A complete, from-zero walkthrough. You'll set up two halves:

1. the **backend add-on** (the voice "brain" that runs the OpenAI Realtime session), and
2. the **device firmware** (turns the Voice PE into a thin client that listens and speaks).

Plan ~30–45 minutes the first time. After that, updates are one click.

```
Home Assistant Voice PE          Home Assistant (your box)             Cloud
┌──────────────────────────┐   plain WS   ┌────────────────────────┐  WS  ┌─────────────┐
│ custom firmware          │ ───────────▶ │ OpenAI Realtime 2      │ ───▶ │ OpenAI      │
│  (va_client thin client) │ 16k mic up   │  Voice Agent add-on    │ 24k  │ Realtime    │
│  wake word + XMOS DSP    │ ◀─────────── │  (Python / pipecat)    │ ◀─── │ API         │
└──────────────────────────┘ 24k spkr dn  │          │ tools       │      └─────────────┘
                                          ▼          ▼
                                 HA MCP Server (/api/mcp) → controls your home
```

## What you need

- A **Home Assistant Voice PE** device (the firmware is **only** for that hardware).
- **Home Assistant OS** (so you can install add-ons).
- An **OpenAI account** with **billing enabled** (the voice runs on OpenAI's paid API).
- A few minutes at the keyboard, and the device on the **same network** as Home Assistant.

---

## Part 1 — The backend add-on (the brain)

### 1.1 Add the repository & install

1. In Home Assistant: **Settings → Add-ons → Add-on Store**.
2. Top-right **⋮ → Repositories** → paste and add:
   `https://github.com/TristanBrotherton/voicepe-realtime`
3. Find **OpenAI Realtime 2 Voice Agent** in the store and click **Install**.
   Home Assistant builds it locally — this takes a few minutes the first time.

> **One add-on instance can serve several Voice PE devices.** See
> [Part 6 — Multiple devices](#part-6--multiple-devices).

### 1.2 Add your OpenAI API key

1. Go to <https://platform.openai.com/> → **API keys** → **Create new secret key**,
   and make sure **billing** is enabled on the account.
2. Open the add-on's **Configuration** tab and paste the key into **`openai_api_key`**.

> New OpenAI accounts start on a low rate-limit tier. If you later see *"Rate limit
> reached"* in the log, raise your usage tier on the OpenAI dashboard, or keep
> `max_context_messages` modest (default 12).

### 1.3 Let it control your home (Home Assistant MCP)

The assistant controls your home through Home Assistant's official, built-in
**[MCP Server](https://www.home-assistant.io/integrations/mcp_server/)** integration —
that's what lets the voice turn your lights, switches, scenes and climate on and off.

1. Add it: **Settings → Devices & Services → Add Integration**, search **"Model Context
   Protocol Server"**, and add it
   ([one-click add](https://my.home-assistant.io/redirect/config_flow_start/?domain=mcp_server)).
2. **Settings → Voice assistants → Expose** → tick the lights, switches, scenes and
   climate you want to control by voice. **Only exposed entities are controllable** —
   this is your safety boundary.
3. In the add-on Configuration, leave **`ha_mcp_url`** and **`longlived_token`**
   **blank**. The add-on then uses Home Assistant's built-in MCP endpoint with its own
   token. (Only fill `longlived_token` if the startup log shows a 401/403 on
   `/core/api/mcp`.)

You get a small fixed set of Assist tools (`HassTurnOn`, `HassTurnOff`, `HassLightSet`,
`GetLiveContext`, `GetDateTime`, …). **`GetLiveContext`** is the "what's the current
state?" tool — keep it; it's what answers *"is the light on?"*.

### 1.4 Minimal configuration

**The defaults are the recommended settings.** For a first run you only need:

| Option | Value |
|---|---|
| `openai_api_key` | your key |
| `transcription_language` | your ISO code (e.g. `en`, `nl`) — optional but recommended |

Everything else can wait. The privacy defaults are conservative: only counters
and wake metadata are stored (no audio), and what you say is not written to the
log. Locks, garage doors, gates and alarm panels need a spoken yes. The full
reference — every option, its default, and when to change it — is in the
[Configuration Reference](configuration.md).

### 1.5 Start it

Click **Start**, then open the **Log** tab. A healthy start shows
`✅ Fetched N MCP tools` and `Creating session with N tools` (with `Hass*` names).
The add-on now listens on port **8080**.

---

## Part 2 — The device firmware

This replaces the stock Home Assistant voice pipeline on the Voice PE with a thin
client that streams audio to the add-on. You set it up **once** via a tiny "stub"
config; after that, firmware updates are one click (no copy-pasting).

The firmware lives in its own repo:
**[TristanBrotherton/voicepe-realtime-firmware](https://github.com/TristanBrotherton/voicepe-realtime-firmware)**.

### 2.1 Install the ESPHome Builder add-on

You build and flash the firmware with the **ESPHome Device Builder** add-on — the
official [ESPHome](https://esphome.io/) tool that runs inside Home Assistant.

1. Open **Settings → Add-ons → Add-on Store**, search **ESPHome Device Builder**, and
   click **Install**
   ([one-click open](https://my.home-assistant.io/redirect/supervisor_addon/?addon=5c53de3b_esphome)).
2. Enable **Show in sidebar**, then **Start** → **Open Web UI**.

### 2.2 Adopt the Voice PE

1. The Voice PE (on its stock firmware) should appear in ESPHome Builder as a
   **discovered device**. If it doesn't, add it by its `home-assistant-voice-xxxx.local`
   address.
2. Click **Adopt**. ESPHome creates a device entry. **Don't install the stock config
   yet.**
3. ESPHome generates an **API encryption key** and an **OTA password** for the device —
   note both; you'll put them in `secrets.yaml` next.

### 2.3 Add your secrets

In ESPHome Builder → **Secrets** (top-right ⋮), add:

```yaml
wifi_ssid: "Your-WiFi"
wifi_password: "your-wifi-password"
ota_password: "the-OTA-password-from-step-2.2"
api_key: "the-API-encryption-key-from-step-2.2"   # 44-char base64

# Optional — ONLY if you want a fixed IP (otherwise the device uses DHCP):
# static_ip: "192.0.2.50"     # use your own LAN's values
# gateway:   "192.0.2.1"
# subnet:    "255.255.255.0"
# dns1:      "1.1.1.1"
# dns2:      "1.0.0.1"
```

Two of these confuse people, so to be clear:

- **`api_key`** is an **ESPHome Noise/API encryption key** — NOT a Home Assistant
  token, NOT your OpenAI key. It's 32 random bytes, base64-encoded. If you flash a
  factory-fresh device (no key from step 2.2), generate your own:
  `openssl rand -base64 32`.
- **`ota_password`** is any password you choose; it protects future over-the-air
  flashes. For an already-adopted device, use the one ESPHome generated so wireless
  updates keep working.

> The firmware itself is pulled from the **public** repo at build time — no token needed.

### 2.4 Paste the device stub & flash

1. In ESPHome Builder, **Edit** the adopted device and **replace its entire YAML** with
   a ready-made stub from the firmware repo:
   - DHCP: [`esphome-builder.dhcp.yaml`](https://github.com/TristanBrotherton/voicepe-realtime-firmware/blob/main/esphome-builder.dhcp.yaml)
   - Fixed IP: [`esphome-builder.static-ip.yaml`](https://github.com/TristanBrotherton/voicepe-realtime-firmware/blob/main/esphome-builder.static-ip.yaml)

   Set `name` and `friendly_name`, and **keep** the `packages:` / `dashboard_import:`
   lines — those are what pull the full firmware from the repo. Keep the device
   `name` stable if you're re-flashing an already-adopted device. Save.

   > Optional: if your add-on isn't reachable at `ws://homeassistant.local:8080/`, add a
   > `va_url:` line under `substitutions:` with your HA host, e.g.
   > `va_url: "ws://192.168.1.x:8080/"`.

2. Click **Install →**
   - **First time:** choose **Plug into this computer** — the first flash from stock
     firmware needs the device connected by **USB** to the machine running your browser.
   - **After that:** **Wirelessly (OTA)** — every later flash goes over Wi-Fi.

That's it — the device boots, connects to the add-on, and you're ready to talk to it.

### 2.5 Lock the device connection (recommended)

Without a token, any computer on your network can open a voice session on the
add-on (spending your OpenAI credit and using the tools you exposed). To require
a shared secret:

1. Generate one, e.g. `openssl rand -hex 24`.
2. Add-on Configuration → 🔐 Privacy & safety: set **`device_token`** to it and
   **`device_auth`** to `permissive`. Restart the add-on.
3. In ESPHome Builder → Secrets add `va_token: "<the same value>"`, and in the
   device stub under `substitutions:` add `va_token: !secret va_token`. Install
   (OTA).
4. The add-on log now shows the device connecting with a valid token. Set
   `device_auth` back to `auto` and restart: devices without the token are
   refused.

---

## Part 3 — First conversation

1. After boot, the LED ring should settle to **idle** — that means the device
   reached the add-on's WebSocket. The add-on log shows `device (re)connected`.
2. Say **"Hey Leonard"** (the default wake word — switch to Hey Jarvis or Okay Nabu
   any time via the device's **"Wake word" dropdown** in Home Assistant, no reflash)
   → a wake chime plays and the ring shows **listening**.
3. Ask for something you exposed, e.g. *"turn on the bedroom lamp"* → the ring shows
   **thinking** → it acts and replies.
4. Keep talking — the mic stays open for a few seconds after each reply, so follow-ups
   need no wake word.
5. To interrupt a reply: say **"stop"** or press the **center button**.

**If something's off, check the logs:**

- Add-on **Log** tab: tool calls, one `⏱️ turn` timing line per turn, and
  `🔌 reconnecting` / `✅ reconnected`. Turn on `log_transcripts` to also see
  `🗣️ user:` / `🤖 assistant:` lines while debugging.
- Device logs: ESPHome Builder → your device → **Logs**.
- Tools missing? Re-check Part 1.3. A 401/403 in the log means set `longlived_token`
  (HA profile → Security).

---

## Part 4 — Updating later (one click)

- **Firmware:** when a new version is released, ESPHome Builder shows **"Update
  available"** for your device. Click it → it recompiles with the latest config + code
  and flashes over Wi-Fi. No copy-pasting, ever again.
- **Add-on:** Home Assistant shows an **Update** badge on the add-on (with a changelog).
  Click **Update** — it rebuilds and restarts.

Your device-specific settings (name, Wi-Fi, IP) live in your stub + `secrets.yaml` and
are **never** overwritten by an update.

---

## Part 5 — Make it yours

Once the basics work, the fun starts. Each of these has a full guide in
[Features](features.md):

- **Persona** — rewrite `instructions` to change personality, language, house rules.
  See [Persona & voices](features.md#persona--voices).
- **Speaker recognition** — set speaker names, then say *"train my voice"* for guided
  enrollment. See [Speaker recognition](features.md#speaker-recognition--voice-enrollment).
- **Memory** — *"remember that the bins go out Thursday"*. See
  [Voice-instructed memory](features.md#voice-instructed-memory).
- **Timers** — set `timer_ring_entity`. See [Voice timers](features.md#voice-timers).
- **Sensors** — set `instance_name` to publish per-device HA sensors. See
  [HA sensors](features.md#ha-sensors).
- **Your own wake word** — [Wake-word learning](wake-word-learning.md).
- **An agent** — deep recall and background task delegation. See
  [Agent Integration](agent-integration.md).

---

## Part 6 — Multiple devices

One add-on instance serves **multiple** Voice PE devices. Point every device's
`va_url` at the same add-on port (for example, `ws://<ha-host>:8080/`). Each
device gets an independent OpenAI session, conversation history, audio stream,
speaker-recognition state, and enrollment flow, so people can use different
rooms at the same time.

All devices in an instance share its configuration: persona, voice, Home
Assistant access, device token, and `instance_name` sensor prefix (the sensors
carry per-device detail in their attributes). Give each device its own timer
bell with `timer_ring_entities`. Run separate add-on instances only when rooms
need different configuration; assign each extra instance a unique
`websocket_port` (avoid `8081`, used by dev builds).
