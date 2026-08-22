# bin/

Drop the **MediaMTX** binary here to enable the anti-drop buffer.

- macOS / Linux: `bin/mediamtx`
- Windows: `bin/mediamtx.exe`

Download the right build for your machine from
<https://github.com/bluenviron/mediamtx/releases> (the Settings page names the
exact file). On macOS/Linux make it executable:

```
chmod +x bin/mediamtx
```

When the binary is present and the buffer is turned on (Settings → Anti-drop
buffer), the app starts and manages MediaMTX for you. If it is missing, the app
just falls back to relaying straight to your RTMP endpoint.

You can also point the app at a binary elsewhere with the **MediaMTX path**
setting instead of using this folder.
