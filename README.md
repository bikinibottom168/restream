# Restream Manager

Relay authorised media inputs to your own RTMP infrastructure, with health
monitoring, automatic recovery and a dark-mode web dashboard.

```
source (provider)  ->  ffprobe validation  ->  FFmpeg  ->  your RTMP endpoint
                              ^                   |
                              |            progress monitor (5 s)
                       refresh this channel  <----+
                          only, on failure    deep source check (5 min)
```

Built for the case where the input URL is **not permanent**: it expires, it
carries a signature, or the origin moves it between nodes. Each channel refreshes
its own URL when it breaks — the other channels keep running untouched.

---

## Contents

- [What this is, and what it is not](#what-this-is-and-what-it-is-not)
- [Requirements](#requirements)
- [Installation](#installation)
  - [macOS](#macos-intel-and-apple-silicon)
  - [Windows 10/11](#windows-1011)
  - [Manual installation](#manual-installation)
- [Configuration (.env)](#configuration-env)
- [First run](#first-run)
- [Providers](#providers)
- [Using the dashboard](#using-the-dashboard)
- [Telegram notifications](#telegram-notifications)
- [How recovery works](#how-recovery-works)
- [Anti-drop buffer (MediaMTX)](#anti-drop-buffer-mediamtx)
- [Start on boot (unattended box)](#start-on-boot-unattended-box)
- [HTTP API](#http-api)
- [Architecture](#architecture)
- [Security notes](#security-notes)
- [Tests](#tests)
- [Troubleshooting](#troubleshooting)
- [เริ่มต้นแบบเร็ว (Thai quick start)](#เริ่มต้นแบบเร็ว)

---

## What this is, and what it is not

**It is** infrastructure for relaying inputs you are entitled to use:
your own encoder, an origin you run, a feed a provider licenses to you, a
camera ingest. You point it at a source, it pushes to an RTMP endpoint you
control, and it keeps that relay alive.

**It is not** a scraper, and it contains no circumvention of any kind. There is
no browser automation, no credential stuffing, no token forgery, no DRM, key
extraction or licence handling. If a manifest declares Widevine, PlayReady,
FairPlay, SAMPLE-AES or DASH ContentProtection, the channel is marked
`UNSUPPORTED` and the pipeline stops there — deliberately, and with no way to
override it.

Where an input needs authentication, you configure the endpoint and the
credentials **you already hold**; the application performs ordinary authorised
requests with them and, when a signed URL expires, asks the same endpoint for a
new one.

---

## Requirements

| | |
|---|---|
| Python | 3.11 or newer |
| FFmpeg + ffprobe | any recent build (7.x, 6.x and 5.x all work) |
| OS | Windows 10/11, macOS (Intel or Apple Silicon), Linux |
| RTMP destination | any RTMP/RTMPS server you control (SRS, nginx-rtmp, MediaMTX, a CDN ingest) |
| Internet | not required by the dashboard — Bootstrap, its icons and HTMX are bundled in `app/static/vendor/`, so the interface loads with no external requests |

Roughly 30 channels on a modern 4-core machine in `copy` mode; copying costs
almost no CPU because nothing is re-encoded. Transcoding is far heavier — budget
one core per 1080p channel.

---

## Installation

The setup scripts are **one-click**: they install the Python dependencies and
**download FFmpeg and MediaMTX into `bin/` for you** (MediaMTX powers the
[anti-drop buffer](#anti-drop-buffer-mediamtx)). The app finds anything in
`bin/` automatically, so there is nothing to add to your PATH. If a download is
blocked or unavailable, the script prints exactly what to fetch and where to put
it, and the dashboard still runs (reporting whatever is missing).

### macOS (Intel and Apple Silicon)

```bash
cd restream-manager
chmod +x setup_macos.sh
./setup_macos.sh        # installs deps; downloads FFmpeg + MediaMTX into bin/

./run_macos.sh          # or double-click run_macos.command in Finder
```

Needs **Python 3.11+**; if it is missing and Homebrew is present, the script
installs it. FFmpeg comes from Homebrew when available, otherwise a static
build is downloaded into `bin/`.

### Windows 10/11

1. Double-click **`setup_windows.bat`**. It will:
   - install **Python 3.12** with `winget` if Python is missing (then asks you
     to reopen and re-run so PATH refreshes),
   - install the Python dependencies,
   - **download FFmpeg and MediaMTX into `bin\`** automatically.
2. Double-click **`run_windows.bat`** and open <http://127.0.0.1:8787>.

Everything lands in the project's `bin\` folder — no PATH editing needed. (You
can still point the app at your own FFmpeg via `FFMPEG_PATH`/`FFPROBE_PATH` in
`.env` if you prefer.)

### Manual installation

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env
python -m app.main
```

Then open <http://127.0.0.1:8787>.

---

## Configuration (.env)

Copy `.env.example` to `.env`. Every value has a working default; nothing is
mandatory to get started.

```env
APP_HOST=127.0.0.1          # keep this on loopback unless you set a password
APP_PORT=8787

CHECK_INTERVAL_SECONDS=300           # deep ffprobe check of the source
PROCESS_MONITOR_INTERVAL_SECONDS=5   # FFmpeg liveness + progress check
FAILURE_THRESHOLD=2                  # failed deep checks before "down"
STALL_TIMEOUT_SECONDS=60             # no output progress = stalled

FFMPEG_PATH=ffmpeg
FFPROBE_PATH=ffprobe

DEFAULT_RTMP_SERVER=                 # rtmp://server.example.com/live/
DEFAULT_STREAM_MODE=copy             # copy | transcode

TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

ADMIN_USERNAME=                      # optional dashboard login
ADMIN_PASSWORD=                      # hashed at startup, never stored plainly

SHOW_FULL_SOURCE_URL=false           # mask tokens in the UI by default
```

Most of these can also be changed at runtime on **Settings** — no restart
needed. Values you edit there are stored in the database and take precedence
over `.env`.

**Never commit `.env`.** It is already in `.gitignore`, along with
`data/secrets.json`, the SQLite database and `logs/`.

---

## First run

1. Start the app; it opens on <http://127.0.0.1:8787> and redirects to
   `/setup` while the database is empty.
2. The setup page checks FFmpeg and ffprobe, and lets you set the default RTMP
   server and Telegram credentials. There are three test buttons: **Test
   FFmpeg**, **Test Telegram**, **Test Provider Login**.
3. Add a provider (**Providers → Add Provider**) *or* skip straight to adding a
   channel with a URL you paste in yourself.
4. Add a channel: name, source, and either a full RTMP URL or a stream key that
   is appended to the default RTMP server.
5. Press **Start**. The channel goes `STARTING → ONLINE` once FFmpeg reports
   that data is flowing to your endpoint.

If FFmpeg is missing, or a provider cannot authenticate, **the dashboard still
starts** and reports the problem in the header — it never blocks on a failed
dependency.

---

## Providers

A provider answers one question: *what is the playable URL for this channel,
right now?* Five are built in.

| Type | Use it when |
|---|---|
| **IPTV** | the easy one — a single form with an optional login and a list where you paste one URL per channel and name each one yourself, many per source |
| **Manual URL** | you paste a media URL per channel |
| **Static M3U8** | a fixed URL, a URL template such as `https://origin/live/{channel_id}/index.m3u8`, or an M3U playlist (which also enables *Sync Channels*) |
| **Per-channel endpoint URL** | every channel has its own page/API URL that returns the media URL — the simplest option when the channels don't follow one pattern |
| **HTTP JSON API** | an authenticated endpoint returns the URL — the right choice when URLs are short-lived and signed |

The **Providers** page shows two worked examples with every field filled in;
press *Use this example* to open the form pre-populated, then change the host
and the field names to match your source.

### The IPTV form (the simplest way)

**Providers → Add IPTV source** is one form that covers the common case:

1. Give the source a name.
2. Tick *This source needs a login* if it does, and fill in the login URL,
   username and password (stored in the OS keychain).
3. In the channel list, paste one URL per row and name each channel — for
   example `https://media.example.com/play?id=82290`. **Add row** for more, or
   **Paste a list** to add many at once (`Name | URL | stream key`).
4. Save. The source logs in once, and every channel's URL is fetched with that
   session, the media URL pulled out of the response, and relayed. A signed URL
   that expires is renewed automatically by fetching its endpoint again.

Behind the scenes this is the HTTP JSON provider with a friendlier form; you can
still open any IPTV source later to edit the login or add more channels.

### One URL per channel

Any channel can carry its own **Source endpoint URL** — a page or API that
*returns* the media URL rather than being the media itself:

```
Sport Channel 01   https://media.example.com/play?id=82290
Sport Channel 02   https://media.example.com/play?id=82291
Test Channel A     https://other.example.com/api/live/testa
```

It is fetched fresh every time the channel is resolved or refreshed, so an
expiring signed URL renews itself. A per-channel endpoint always overrides the
provider's URL template, so one odd channel does not force a second provider.

Set the JSON path (for example `data.stream.url`) when you want to be strict
about which field is read; leave the parser on `auto` and the whole response is
searched, including HTML pages and JavaScript blobs.

### Bulk add

**Dashboard → Bulk Add** takes a list and creates one channel per URL:

```
Sport Channel 01 | https://media.example.com/play?id=82290 | sport01
Sport Channel 02 | https://media.example.com/play?id=82291 | sport02
Test Channel A   | https://origin.example.com/live/testa/index.m3u8
```

Columns are `Name | URL | stream key`, separated by `|`, a tab, a comma or a
semicolon. Bare URLs work too — the name is derived from the URL. So does a
JSON file:

```json
[
  {"name": "Sport Channel 01", "url": "https://media.example.com/play?id=82290",
   "stream_key": "sport01"}
]
```

A URL ending in `.m3u8`, `.mpd`, `.ts` (or an `rtmp://`/`srt://` address) is
stored as the channel's direct input; anything else becomes its endpoint.
**Preview** shows exactly how each line was understood before anything is
saved, and duplicates are skipped.

### The HTTP JSON provider

Nothing about the endpoint is hardcoded. You configure it on the Providers page:

```json
{
  "base_url": "https://media.internal.example",
  "auth": {
    "type": "form",
    "url": "/login",
    "username_field": "username",
    "password_field": "password"
  },
  "channels": {
    "url": "/api/channels",
    "list_path": "data",
    "id_field": "id",
    "name_field": "name"
  },
  "stream": {
    "url": "/api/play?id={channel_id}",
    "url_path": "data.stream.url"
  }
}
```

- **Authentication types**: `none`, `basic`, `bearer`, `form`, `cookie`,
  `headers`.
- **Response parsers**: `json_path` (dotted paths like `data.stream.url`,
  including `items[0].url` and `items[*].url`), `text` (body is the URL),
  `location` (follow the redirect), or `auto` — which handles JSON, HTML,
  JavaScript blobs and escaped URLs (`https:\/\/host\/x.m3u8`), and falls back
  to scanning an embedded player one level deep.
- **Session handling**: one HTTP client per provider holds the cookie jar. A
  `401`, `403` or redirect-to-login triggers exactly one re-authentication,
  serialised behind a lock — thirty channels produce one login, not thirty.
- **Credentials** (`username`, `password`, `token`, `cookie`) go to the OS
  keychain: Keychain on macOS, Credential Manager on Windows. If no keychain is
  available they land in `data/secrets.json` with `0600` permissions. They are
  never stored in the database, never returned by the API, and never exported.

Three buttons per provider exercise the whole path: **Test Authentication**,
**Test Channel List**, **Test Stream Resolver** (which resolves, then runs
ffprobe, and reports codec/resolution). `/providers/{id}/debug` shows HTTP
status, content type, a response preview and the cookies received — all masked.

### Your own provider

Drop a file in `app/providers/custom/` and it is registered automatically. See
**[docs/CREATE_PROVIDER.md](docs/CREATE_PROVIDER.md)** — there is a working
example in `app/providers/custom/example_provider.py.txt`.

---

## Using the dashboard

The interface is available in **Thai and English** — switch with the ไทย/English
buttons in the top-right corner, or set `UI_LANGUAGE=th` (the default) or `en`
in `.env`. The choice is stored and applies immediately, no restart needed.

**Home** — cards for Total / Online / Offline / Reconnecting / Disabled, live
CPU, RAM, FFmpeg process count and application uptime, then the channel table:
status, source, RTMP, uptime, bitrate, last check, restarts. It refreshes itself
every few seconds through HTMX; the page never reloads.

Search and filter (All / Online / Offline / Reconnecting / Disabled) sit above
the table. Bulk actions: **Start All**, **Stop All**, **Restart Selected**,
**Refresh Selected**, **Sync Channels** — the destructive ones ask first.

**Channel detail** (`/channels/{id}`) — everything about one relay: current
source (with a Copy button), last refresh, expiry, resolve count, RTMP
destination, FFmpeg PID and status, output time, bitrate, speed, seconds since
the last progress update, restart counts, last error, recent events, downtime
history and the tail of that channel's FFmpeg log. Per-channel actions include
**Test Source**, which resolves and probes without touching the running stream.

**Events** (`/events`) — every state change, source refresh, login result and
system message. **History** (`/history`) — one row per outage with duration,
cause and attempt count, plus a seven-day summary so a repeatedly failing
channel is obvious. **Logs** (`/logs`) — tails `logs/app.log` or any channel's
FFmpeg log; only the tail is read, never the whole file.

**Settings** (`/settings`) — everything tunable at runtime, plus **Export
Configuration** / **Import Configuration** (channels, providers and settings —
never credentials).

---

## Telegram notifications

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (in `.env` or on Settings), then
press **Send Test Message**.

Messages are sent **on state transitions only**, never on a timer:

```
🔴 STREAM DOWN                    🟢 STREAM RECOVERED
Channel: Sport Channel 01         Channel: Sport Channel 01
Time: 22/08/2026 00:35            Downtime: 48s
Reason: source unreachable        Attempts: 2
                                  RTMP streaming normally.
Refreshing the source URL...
```

A channel that is down for two hours produces exactly one DOWN message.
Retries in between are silent — the dashboard already shows them. Also sent:
**channel unstable** (once per trip of the restart circuit), **provider
authentication error** and **system error**, both rate-limited to one every 15
minutes.

---

## How recovery works

Two independent loops watch every channel:

- **FFmpeg process monitor — every 5 seconds.** Is the process alive? Is
  `out_time` still advancing? A live process is not proof of a working stream,
  so a stalled one (no progress for `STALL_TIMEOUT_SECONDS`) counts as failed.
- **Deep source check — every 5 minutes.** One ffprobe against the current
  source URL. A single timeout does not restart anything: it takes
  `FAILURE_THRESHOLD` consecutive failures (default 2) to declare the channel
  down. In between it shows as `DEGRADED`.

When a failure is confirmed, for **that channel only**:

```
mark RECONNECTING  ->  stop its FFmpeg  ->  ask the provider for a new URL
   ->  validate with ffprobe  ->  start FFmpeg  ->  confirm output is flowing  ->  ONLINE
```

Retry delays follow `3s, 5s, 10s, 20s, 30s…`, capped by
`MAX_RESTART_DELAY_SECONDS` and reset the moment the channel returns. More than
`RESTART_WINDOW_THRESHOLD` restarts inside `RESTART_WINDOW_SECONDS` trips a
circuit breaker: retries slow to `UNSTABLE_RESTART_DELAY_SECONDS` and you are
told once that the channel is unstable.

If the provider session has expired, the provider re-authenticates once and the
request is retried. Channels that are still working are **not** restarted
because a session expired elsewhere.

On startup the app checks for FFmpeg processes left behind by a crash and
terminates them — but only those it can prove it started, by matching pid,
process creation time and the exact argument vector. Another application's
FFmpeg is never touched.

`Ctrl+C` stops the watchdog, ends every FFmpeg child, closes the HTTP clients
and the database, and leaves no zombie processes, on both Windows and macOS.

---

## Anti-drop buffer (MediaMTX)

By default each channel relays straight to your RTMP endpoint, so a source
dropout is passed through to whoever is watching and their player disconnects.
Turn on the **anti-drop buffer** (Settings → *บัฟเฟอร์กันหลุด* / Anti-drop buffer)
to put a small local media server between the ingest and your viewers:

```
source m3u8 --(copy)--> FFmpeg ingest --> MediaMTX --> VLC / players
   (can drop)          (auto-recovers)   (holds a 30s buffer,
                                          keeps the viewer connected)
```

* **Short dropouts become invisible.** Viewers play ~30s behind live (tunable,
  0–300s), so anything shorter than the buffer is never seen.
* **The viewer connection never drops.** MediaMTX holds the player session
  itself, independently of the ingest FFmpeg — so re-login, a fresh signed URL,
  or a full ingest restart happens behind the scenes while playback continues.
* **Long outages show a "reconnecting" screen** instead of a dead player — and
  only for the channels that are actually down, so the extra CPU is bounded
  (important on a modest machine). Point it at your own PNG or leave it as a
  plain dark screen.
* **Copy-friendly.** The main path stays `-c copy`; nothing is transcoded per
  channel just to gain the buffer.

**Viewer URLs.** With the buffer on, each channel page shows a **Watch links**
card. Point VLC at the **HLS** link (`http://<host>:8888/ch<id>/index.m3u8`) for
the most resilient playback; an **RTMP** link is offered too for OBS-style
players.

### Installing MediaMTX

MediaMTX is a single self-contained binary (Windows and macOS supported). The
Settings page tells you the exact file to download for your machine; put the
binary in the project's `bin/` folder (or set **MediaMTX path**):

```
<project>/bin/mediamtx        # macOS / Linux
<project>\bin\mediamtx.exe    # Windows
```

Download it from the MediaMTX releases page
(<https://github.com/bluenviron/mediamtx/releases>). When the binary is present
and the buffer is enabled the server starts automatically; if it is missing the
app logs a clear message and simply falls back to the direct-RTMP path, so
nothing breaks in the meantime.

Ports (all configurable): RTMP `1935`, HLS `8888`, API `9997`. Set **Viewer
host / IP** to the address players use to reach the machine (e.g. your LAN IP).

---

## Start on boot (unattended box)

For a machine that should run the relay around the clock, enable **auto-start**
so the app comes back on its own after a reboot or power cut, and restarts
itself if it ever crashes — no administrator rights required:

- **From the dashboard:** Settings → *เปิดอัตโนมัติเมื่อเปิดเครื่อง* / Start on
  boot → **Install auto-start** (and **Remove auto-start** to undo).
- **Windows:** `setup_windows.bat` offers to enable it, or run
  `autostart_install.bat` / `autostart_remove.bat` any time. It registers a
  per-user Scheduled Task that starts at logon, has no time limit, and restarts
  the app every minute if it stops. It runs windowless via `pythonw.exe`.
- **macOS:** a LaunchAgent in `~/Library/LaunchAgents` with `RunAtLoad` +
  `KeepAlive` (login start + crash restart).

Because it starts at **logon**, a box that is expected to recover unattended
should also be set to log the user in automatically after boot.

---

## HTTP API

Everything the dashboard does is available as JSON.

```
GET    /health                          liveness + channel counts
GET    /api/status                      summary, system metrics, binaries
GET    /api/channels                    all channels + live state
GET    /api/channels/{id}
POST   /api/channels                    create
PUT    /api/channels/{id}               update
DELETE /api/channels/{id}
POST   /api/channels/{id}/start|stop|restart|refresh|test|enable|disable
POST   /api/channels/bulk               {"action": "...", "channel_ids": [...]}
GET    /api/providers                   providers + available types
POST   /api/providers                   create      PUT/DELETE /api/providers/{id}
POST   /api/providers/{id}/test-auth|test-channels|test-resolve
GET    /api/providers/{id}/debug        masked request/response trace
POST   /api/test-login                  authenticate the default provider
POST   /api/sync                        discover channels from providers
POST   /api/telegram/test
GET    /api/events   /api/history   /api/logs
GET    /api/settings                    POST to update
GET    /api/config/export               POST /api/config/import
WS     /ws/status                       status frame every 3 seconds
```

Interactive documentation lives at `/api/docs`.

```bash
curl http://127.0.0.1:8787/health
# {"status":"ok","ffmpeg":true,"ffprobe":true,"channels_online":15,"channels_total":18}
```

---

## Architecture

```
app/
  main.py                 FastAPI app + lifespan (startup / shutdown order)
  core/
    config.py             pydantic-settings, validated at startup
    settings_store.py     runtime overrides layered over the environment
    secrets.py            OS keychain, with a 0600 file fallback
    security.py           masking, scrubbing, PBKDF2 password hashing
    logging.py            rotating app log + one rotating log per channel
    state.py              channel state machine
    timeutil.py
  providers/              <-- everything source-specific lives here
    base.py               StreamProvider, ChannelInfo, ResolvedStream
    factory.py            ProviderFactory + custom provider autoloading
    manager.py            one live instance per provider row
    resolver.py           resolve -> DRM check -> ffprobe -> persist
    jsonpath.py           data.stream.url, items[0].url, items[*].url
    extract.py            URL extraction from JSON/HTML/JS + DRM detection
    manual.py static_m3u8.py http_json.py
    custom/               drop your own provider in here
  streaming/              <-- knows nothing about providers
    ffmpeg.py             command building, -progress parsing, safe shutdown
    supervisor.py         one instance + one asyncio.Task per channel
    manager.py            orchestration, watchdog, sync, system metrics
    probe.py              ffprobe validation, binary detection
    backoff.py            retry ladder + restart circuit breaker
    orphan.py             provable identification of our own processes
  notifications/telegram.py
  database/               SQLAlchemy 2.0 models, CRUD, engine
  web/                    routes, JSON API, websocket, schemas
  templates/  static/
data/   logs/   tests/   docs/
```

The dependency arrow runs one way: `streaming` imports from `providers`, never
the reverse. Adding a provider cannot require a change in the streaming layer,
the dashboard or the health monitor.

Background work uses `asyncio` throughout — one task per channel, plus one
watchdog — started from the FastAPI lifespan. There is no blocking
`while True: time.sleep()` anywhere, and no request handler ever waits on a
stream. Synchronous SQLAlchemy calls are dispatched to a worker thread so the
event loop is never blocked.

---

## Security notes

- The dashboard binds to `127.0.0.1` by default. Change `APP_HOST` only after
  setting `ADMIN_USERNAME` and `ADMIN_PASSWORD`.
- Dashboard passwords are hashed with PBKDF2-HMAC-SHA256 (240k rounds) and
  compared in constant time. Plaintext is never written anywhere.
- Provider credentials live in the OS keychain, or in `data/secrets.json` with
  `0600` permissions when no keychain is available.
- Signed URL parameters (`token`, `signature`, `expires`, `key`, …) are masked
  in the dashboard, in logs and in Telegram messages. Set
  `SHOW_FULL_SOURCE_URL=true` if you need the real URL for debugging.
- `Authorization` headers, cookie values and passwords are masked before any
  log line is written, including FFmpeg's own output.
- Configuration exports deliberately exclude every credential.
- FFmpeg is spawned with `create_subprocess_exec`, never through a shell, so a
  URL containing shell metacharacters is inert.

---

## Tests

```bash
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pytest -q
```

173 tests, no network access — provider tests run against
`httpx.MockTransport`. Coverage includes configuration validation, the state
machine, the backoff ladder and circuit breaker, Telegram de-duplication, URL
extraction from JSON/HTML/JavaScript fixtures, DRM detection, JSON path
resolution, M3U parsing, provider session handling (including
re-authentication after a 401), FFmpeg command construction, secret masking and
the database layer.

Fixtures for offline development live in `tests/fixtures/`
(`live_page.html`, `play_response.json`, `channels.m3u`).

---

## Troubleshooting

**"FFmpeg Not Installed" in the header**
`ffmpeg` is not on PATH. Set `FFMPEG_PATH` and `FFPROBE_PATH` in `.env` to the
full executable paths, or on the Settings page.

**A channel sits at `CONFIG REQUIRED`**
No RTMP destination. Add a full `rtmp_url` on the channel, or set
`DEFAULT_RTMP_SERVER` on Settings plus a stream key on the channel. FFmpeg is
deliberately never started without a destination.

**A channel sits at `UNSUPPORTED`**
The source declares DRM or sample-level encryption. This application implements
no circumvention; use an input you can decrypt legitimately at source.

**`Error opening output files: Connection refused`**
The RTMP endpoint is not accepting the connection — wrong URL, wrong stream key,
or the media server is not running. Test it with:
```bash
ffmpeg -f lavfi -i testsrc2 -t 10 -c:v libx264 -f flv rtmp://your-server/live/key
```

**Channel restarts in a loop, then slows down**
The circuit breaker tripped after too many restarts in the window. Look at the
channel's FFmpeg log (`/logs?source=channel`) — usually a source that keeps
ending, or codecs FLV cannot carry.

**`copy` mode fails but the source plays elsewhere**
RTMP/FLV cannot carry every codec (HEVC and Opus, for instance). Switch that
channel's Stream Mode to **Transcode**.

**Audio drifts or is missing after copying from MPEG-TS**
Already handled: `-bsf:a aac_adtstoasc` is added automatically when ffprobe
reports AAC. If the source is not AAC, use Transcode.

**Dashboard is slow with many channels**
Raise `PROCESS_MONITOR_INTERVAL_SECONDS` slightly (5 → 10) and
`CHECK_INTERVAL_SECONDS` if you run well beyond 30 channels.

**Where are the logs?**
`logs/app.log` (10 MB × 5 rotations) and `logs/ffmpeg/<channel_id>.log` per
channel. Both are readable from `/logs`.

---

## เริ่มต้นแบบเร็ว

**macOS**

```bash
brew install ffmpeg
chmod +x setup_macos.sh && ./setup_macos.sh
./run_macos.sh
```

**Windows** — ติดตั้ง Python 3.11+ และ FFmpeg (ตั้ง PATH หรือใส่ `FFMPEG_PATH`
ใน `.env`) แล้วดับเบิลคลิก `setup_windows.bat` ตามด้วย `run_windows.bat`

เปิด <http://127.0.0.1:8787> แล้วทำตามนี้:

1. หน้า `/setup` จะตรวจ FFmpeg ให้ และให้กรอก RTMP server หลัก + Telegram
2. ไปที่ **Providers** เพื่อเพิ่มแหล่งที่มาของ URL
   (หรือข้ามไปเลย แล้วใช้แบบ **Manual** วาง URL เองรายช่อง)
3. กด **Add Channel** ใส่ชื่อ, source, และ RTMP ปลายทาง (หรือ stream key)
4. กด **Start** — ระบบจะ resolve URL → ตรวจด้วย ffprobe → เปิด FFmpeg →
   ยืนยันว่ามีข้อมูลไหลออก RTMP จริง แล้วขึ้นสถานะ `ONLINE`

ถ้าต้นทางล่ม ระบบจะแจ้ง Telegram หนึ่งครั้ง, ขอ URL ใหม่ **เฉพาะช่องนั้น**,
เปิด FFmpeg ใหม่ และแจ้ง `RECOVERED` พร้อมระยะเวลาที่ล่มเมื่อกลับมาปกติ
ช่องอื่นที่ทำงานอยู่จะไม่ถูกแตะต้อง

การเพิ่ม provider ของตัวเอง อ่าน **[docs/CREATE_PROVIDER.md](docs/CREATE_PROVIDER.md)**

---

Restream Manager v1.2.0
