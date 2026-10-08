# Deployment guide

## 1. Telegram setup (do this first)
1. **Create the bot** with @BotFather. Keep the token. Privacy mode can stay ON: commands (`/play ...`) are still delivered to bots in groups.
2. **Create the assistant account**: a normal user account (a spare number, not your main one). Run `generate_session.py` locally (`API_ID`/`API_HASH` from https://my.telegram.org) and keep the printed string as `ASSISTANT_SESSION`. It gives full access to that account; treat it like a password.
3. **Music group**: add the bot AND the assistant account as members. Start a voice chat in the group (or let an admin do it). Making the bot admin does **not** give the assistant any rights; add the assistant separately. If members cannot start voice chats in your group, the assistant needs the "Manage video chats" admin right to join an existing one smoothly, and an admin must start the chat.
4. **Storage channel** (private is fine): add the bot as admin with **Post messages**; add the assistant account as a member or admin so it can read history. Use the real ids (`-100...`): forward a message to @RawDataBot or check the web URL.
5. Check both accounts can see `GROUP_ID` and `STORAGE_CHANNEL_ID`; the bot refuses to start otherwise and says which one failed.

## 2. Environment variables
| Variable | Meaning |
|---|---|
| `API_ID`, `API_HASH` | from my.telegram.org |
| `BOT_TOKEN` | from BotFather |
| `ASSISTANT_SESSION` | Pyrogram string session of the assistant |
| `GROUP_ID` | music group id (negative, `-100...`) |
| `STORAGE_CHANNEL_ID` | cache channel id (negative, `-100...`) |
| `COOKIES_PATH` | optional Netscape cookie file, e.g. `/etc/secrets/cookies.txt` |
| `DOWNLOAD_DIR` | `/var/data/downloads` with a disk, otherwise `/app/downloads` |
| `CACHE_SCAN_LIMIT` | how many recent channel messages to scan per lookup (default 500) |
| `LOG_LEVEL` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `MAX_VIDEO_HEIGHT`, `MAX_DISK_MB` | optional; defaults 480 and 2000 |

## 3. Render
1. Push this folder to a **private** GitHub repo (`.gitignore` already excludes `.env` and `cookies.txt`).
2. Dashboard: **New -> Background Worker** (not Web Service; a worker has no port, so "No open ports detected" cannot occur). Runtime: **Docker**, Dockerfile path `./Dockerfile`. Or use **New -> Blueprint** with `render.yaml`.
3. Start command: leave empty (Dockerfile `CMD ["python","-u","bot.py"]`). If you must set one: `python -u bot.py`.
4. Instance type: a paid type (required for disks).
5. **Disk** (optional but recommended): name `bot-data`, mount path `/var/data`, then `DOWNLOAD_DIR=/var/data/downloads`. Without a disk, downloaded files vanish on every deploy/restart. This is harmless for correctness (anything cached in the channel is re-fetched from Telegram), but costs time and bandwidth. A disk limits you to one instance and brief downtime during deploys: do not run two copies, both would use the same assistant session.
6. Add every variable from the table under **Environment**.
7. Deploy. Healthy startup logs: `Versions: ...`, `Assistant can read storage channel`, `Bot is up.`.

## 4. YouTube cookies (optional, not a guarantee)
YouTube often blocks datacenter IPs with "Sign in to confirm you're not a bot". Cookies from a logged-in browser session can help; they do not always work and can expire.
1. Use a **throwaway Google account** (automation can get accounts restricted).
2. Export cookies in **Netscape format** (first line `# Netscape HTTP Cookie File`) with a browser extension that exports `cookies.txt`, following yt-dlp's current guide: https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp
3. Render dashboard -> your worker -> **Environment -> Secret Files** -> add file `cookies.txt` with the contents. It is mounted at `/etc/secrets/cookies.txt`; set `COOKIES_PATH=/etc/secrets/cookies.txt`.
4. Never commit the file. The bot copies it to a private temp file (secret files are read-only but yt-dlp rewrites its cookie jar) and never logs its contents.

## 5. Testing checklist
- [ ] Logs show `Bot is up.` with no `SystemExit` message.
- [ ] `/play <song name>` -> status updates, audio plays in the voice chat, a new audio message with a 4-line caption appears in the storage channel.
- [ ] Same `/play` again -> log says `Cache hit (audio)`; no YouTube download.
- [ ] `/vplay <name>` -> video plays; channel message is a *video* with `Type: video`; `/play` of the same name does NOT hit it.
- [ ] `/play` while playing -> "Queued at #n"; `/queue` and `/now` look right.
- [ ] `/pause`, `/resume`, `/skip` (next track starts, queue intact), `/stop` (leaves chat, queue cleared), then `/play` works again.
- [ ] Track ending naturally starts the next one exactly once.
- [ ] No active voice chat -> friendly error, bot keeps running.
- [ ] Redeploy: bot restarts cleanly (SIGTERM handled), cache still works.
