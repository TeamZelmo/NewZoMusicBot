# Troubleshooting

**`RuntimeError: Task got Future attached to a different loop`**
Cause: a Pyrogram/PyTgCalls object created on one event loop and awaited on another. Typical triggers: clients built at import time (Pyrogram stores `get_event_loop()` in `Client.__init__`) and then used inside `asyncio.run()`; `app.run()`/`idle()` mixed with `asyncio.run`; Pyrogram or PyTgCalls methods called from `asyncio.to_thread`/other threads (Pyrogram's sync wrapper then hops to the loop captured at import time). Fix (already in `bot.py`): create, start and use all clients inside the one coroutine run by `asyncio.run(...)`; threads only for yt-dlp/file I/O. If you extend the bot, never call Pyrogram/PyTgCalls from a thread and never call `asyncio.run` in a handler.

**`BOT_METHOD_INVALID`**
A bot token called a user-only method (channel history). History scans use the assistant client. If you see it, something calls `bot.get_chat_history` / `search_messages`.

**`Sign in to confirm you're not a bot`**
YouTube blocked the server IP. Add cookies (DEPLOYMENT.md section 4), update `yt-dlp` (rebuild the image), confirm `deno --version` works in the container. Not always solvable from a cloud IP; cached items keep working.

**`No open ports detected`**
The service is a Web Service. Recreate it as a **Background Worker** (or `type: worker` in `render.yaml`). Do not add a fake HTTP server.

**Voice chat connection failures**
- "no active voice chat": start one in the group, then `/play`.
- Assistant not in the group, or restricted/banned: add/unban it.
- `Timeout` on play: check Render logs for ffmpeg/NTgCalls errors; retry; try `/play` with a short audio first.
- PyTgCalls 3.0.0 is brand-new (PyPI release 25 Sep 2026) and was not verified against this code end to end; if `VoicePlayer` reports a missing API, adjust only that class.

**Storage channel permission errors**
`Assistant account cannot read STORAGE_CHANNEL_ID`: join the assistant to the channel; check the `-100...` id. `ChatAdminRequired` / `ChatWriteForbidden` on upload: make the bot admin with "Post messages". Playback still works, caching does not.

**Missing FFmpeg** (`ffmpeg not found on PATH`): you are not running the Dockerfile image; the Dockerfile installs it.

**Missing/invalid session string**: startup lists `ASSISTANT_SESSION` as invalid, or `Telegram login failed (AuthKeyUnregistered...)`: regenerate with `generate_session.py` (the session dies if you log the account out or terminate the session).

**Missing media files**: the bot prunes old downloads beyond `MAX_DISK_MB`, never queued/current ones. Without a disk, a restart empties the queue anyway (queue is in memory).

**Failed cache lookups**
Log line `Cache lookup FAILED (not a miss)` = history could not be read; the bot falls back to YouTube. `Cache miss ... after scanning N` = genuinely not in the last `CACHE_SCAN_LIMIT` messages; raise the limit if the channel is large. Entries need the 4-line caption and matching media type (audio vs video).

**Reading logs on Render**: dashboard -> service -> Logs; set `LOG_LEVEL=DEBUG` temporarily; secrets are redacted but never paste logs publicly without checking.
