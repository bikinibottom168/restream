# How to create your own StreamProvider

The streaming half of this application — `StreamSupervisor`, `FFmpegManager`,
the health monitor, the dashboard — never learns where a URL came from. It only
ever sees a `ResolvedStream`. That means adding a new source is one new file and
nothing else changes.

---

## 1. The contract

Every provider subclasses `app.providers.base.StreamProvider` and may implement
five methods:

```python
class StreamProvider:
    async def authenticate(self) -> bool: ...
    async def list_channels(self) -> list[ChannelInfo]: ...
    async def resolve_stream(self, channel: ChannelInfo) -> ResolvedStream: ...
    async def refresh_stream(self, channel: ChannelInfo) -> ResolvedStream: ...
    async def health(self) -> ProviderHealth: ...
```

Only `resolve_stream` is mandatory. The rest have working defaults:

| Method | Default behaviour | Override when |
|---|---|---|
| `authenticate` | marks the provider authenticated, does nothing | your source needs a login, token exchange or session |
| `list_channels` | raises `DiscoveryNotSupported` | your source can enumerate channels (enables **Sync Channels**) |
| `refresh_stream` | calls `resolve_stream` | you cache internally and a refresh must bypass that cache |
| `health` | reports OK | you can cheaply check reachability |

Lifecycle hooks: `start()` (allocate an HTTP client) and `aclose()` (release it).
Both are called by `ProviderManager`; `aclose()` must never raise.

---

## 2. The data model

### `ChannelInfo` — what the provider is asked about

```python
ChannelInfo(
    id="82290",              # the provider-side id (channels.provider_ref)
    name="Sport Channel 01",
    logo="https://.../logo.png",
    metadata={               # free-form; the built-ins use these keys
        "input_url": "...",  # URL the operator typed on the channel
        "referer": "...",
        "user_agent": "...",
        "headers": {...},
        "group_title": "...",
    },
)
```

`ChannelInfo.local_id` holds the local database primary key when the channel
exists locally. Use `id` for provider calls, `local_id` for logging.

### `ResolvedStream` — what the provider returns

```python
ResolvedStream(
    channel_id="82290",
    url="https://edge/live/x/index.m3u8?token=...",
    headers={"X-Session": "..."},   # extra headers FFmpeg must send
    cookies={"sid": "..."},         # merged into a Cookie header
    referer="https://portal.example/",
    user_agent="ExamplePlayer/1.0",
    expires_at=datetime(...),       # aware UTC, or None
    provider="my_provider",
    note="anything useful for the UI",
)
```

`request_headers()` merges cookies, referer and user-agent into one header map.
`FFmpegManager` passes that map to FFmpeg via `-headers` / `-user_agent`, so an
input that only serves media to a properly-headed request works unchanged.

**Never treat a URL as permanent.** Set `expires_at` when you know the lifetime;
the supervisor re-resolves before it lapses and, when a stream breaks, calls
`refresh_stream()` for that channel and restarts only that channel's FFmpeg.

---

## 3. Errors — pick the right one

| Exception | Meaning | What the supervisor does |
|---|---|---|
| `ProviderAuthError` | session/credentials rejected (401, 403, login redirect) | records a provider auth failure, notifies once, retries with backoff |
| `ProviderUnavailable` | reachable, but no usable stream right now | retries this channel with backoff |
| `ProviderUnsupportedMedia` | needs DRM/licence handling | marks the channel `UNSUPPORTED` and **stops** — no retry |
| `ProviderConfigError` | the stored configuration is incomplete | surfaced in the UI; no retry loop |
| `DiscoveryNotSupported` | `list_channels` is not implemented | Sync Channels reports it politely |

Anything else is caught, logged with a traceback and treated as unavailable —
a bug in a provider can never take the process down or stall other channels.

---

## 4. Write the file

Copy `app/providers/custom/example_provider.py.txt` to
`app/providers/custom/my_provider.py` and edit it. Everything in
`app/providers/custom/` is imported at startup and every `StreamProvider`
subclass found there is registered automatically — no imports to add, no
factory to edit.

Minimum viable provider:

```python
from app.providers.base import ChannelInfo, ResolvedStream, StreamProvider


class MyProvider(StreamProvider):
    type_name = "my_provider"        # must be unique
    label = "My source"

    async def resolve_stream(self, channel: ChannelInfo) -> ResolvedStream:
        return ResolvedStream(
            channel_id=channel.id,
            url=f"https://origin.example/live/{channel.id}/index.m3u8",
            provider=self.type_name,
        )
```

Restart the app; "My source" appears in the provider-type dropdown.

If you would rather register explicitly (for a provider living outside
`custom/`), call the factory once at import time:

```python
from app.providers.factory import ProviderFactory

ProviderFactory.register(MyProvider)
```

---

## 5. Configuration and credentials

`self.config` is the JSON blob stored on the provider row. Read nested values
with `self.option("auth.type", "none")`.

Describe your fields with `config_schema()` and the dashboard renders a proper
form instead of a raw JSON box:

```python
@staticmethod
def config_schema() -> list[dict]:
    return [
        {"key": "base_url", "label": "Base URL", "type": "url", "required": True},
        {"key": "auth.type", "label": "Auth", "type": "choice",
         "choices": ["none", "bearer"]},
        {"key": "headers", "label": "Extra headers (JSON)", "type": "json"},
    ]
```

Supported field types: `text`, `url`, `number`, `bool`, `choice` (with
`choices`), `json`. Optional keys: `default`, `help`, `required`.

**Credentials never go in `config`.** Four secret slots — `username`,
`password`, `token`, `cookie` — are stored in the OS keychain (macOS Keychain /
Windows Credential Manager) or, if no keychain is available, in
`data/secrets.json` with `0600` permissions. Read them with `self.secret("password")`.
They are never written to the database, never returned by the API, and never
appear in a log or an export.

---

## 6. Rules the built-ins follow (and yours should too)

1. **Mask secrets.** Use `mask_url_token()` and `mask_secret()` from
   `app.core.security` before logging anything. `register_secret(value)` makes a
   literal disappear from every log line and every Telegram message.
2. **Refuse protected media.** If a manifest declares Widevine, PlayReady,
   FairPlay, SAMPLE-AES or DASH ContentProtection, raise
   `ProviderUnsupportedMedia`. `detect_drm()` in `app.providers.extract` does the
   check. This project implements no DRM, key extraction or licence handling,
   and a provider must not add any.
3. **Never forge a token.** When a signed URL expires, ask the source for a new
   one through the same endpoint. Do not construct, extend or reverse-engineer a
   signature.
4. **One session, many channels.** Thirty channels share one provider instance.
   Keep one `httpx.AsyncClient`, guard login with an `asyncio.Lock`, and let
   waiters reuse the session rather than each logging in. `HttpJsonProvider` is
   the reference implementation.
5. **Be honest about failure.** Raise the specific exception; the supervisor's
   recovery behaviour depends on which one it receives.

---

## 7. Test it without the network

Provider tests use `httpx.MockTransport` — see `tests/test_providers.py`:

```python
def handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/login":
        return httpx.Response(200, json={"ok": True})
    return httpx.Response(200, json={"data": {"stream": {"url": "https://cdn/a.m3u8"}}})

provider = MyProvider(config={"base_url": "https://portal.example"})
provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
stream = await provider.resolve_stream(ChannelInfo(id="1", name="A"))
assert stream.url == "https://cdn/a.m3u8"
```

Worth covering: a happy resolve, a 401 that triggers exactly one re-login, a
response with no URL, and expiry parsing.

---

## 8. Check it from the dashboard

On **Providers** each row has three buttons that exercise your code in order:

- **Test Authentication** → `authenticate()`
- **Test Channel List** → `list_channels()`
- **Test Stream Resolver** → `resolve_stream()` followed by ffprobe, reporting
  reachability, video codec, audio codec and resolution

**Debug** (`/providers/{id}/debug`) shows HTTP status, content type, a response
preview, the cookies received and the parsed channel count — with passwords,
`Authorization`, cookie values, tokens and signatures masked.
