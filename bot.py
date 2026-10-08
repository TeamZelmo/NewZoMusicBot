#!/usr/bin/env python3
"""Telegram music bot.

Architecture (one process, ONE asyncio event loop):

  * bot client        -> receives commands, replies, uploads media to the storage channel
  * assistant client  -> user session; joins the voice chat through PyTgCalls and reads the
                         storage channel history (bots cannot: BOT_METHOD_INVALID)
  * cache             -> the Telegram storage channel itself (captions carry the metadata)

EVENT-LOOP RULE (this is what fixes "Future attached to a different loop"):
  Every Pyrogram client and the PyTgCalls instance are CREATED, STARTED and USED inside the
  single coroutine started by ``asyncio.run(...)`` in ``main()``. Nothing in this file creates
  a second loop, calls ``asyncio.run`` again, or touches Pyrogram/PyTgCalls from a worker
  thread. Worker threads (``asyncio.to_thread``) are used ONLY for yt-dlp and file I/O.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Awaitable, Callable, Optional

from dotenv import load_dotenv
from pyrogram import Client, enums, filters
from pyrogram.errors import RPCError
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message
import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

log = logging.getLogger("musicbot")

MIN_PLAY_SECONDS = 2.0       # stream-end events sooner than this after a start are treated as stale/duplicate
QUEUE_PAGE = 10              # max queue rows shown in /queue
VOICE_TIMEOUT = 60           # seconds allowed for PyTgCalls play()
PRUNE_MIN_AGE = 600          # never prune files younger than this (seconds)


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    assistant_session: str
    group_id: int
    storage_channel_id: int
    cookies_path: Optional[str]
    download_dir: Path
    cache_scan_limit: int
    log_level: str
    max_video_height: int
    max_disk_mb: int

    def secrets(self) -> list[str]:
        return [self.api_hash, self.bot_token, self.assistant_session]


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def load_config() -> Config:
    load_dotenv()
    problems: list[str] = []

    def need_int(name: str) -> int:
        raw = _env(name)
        if raw is None:
            problems.append(f"{name} is missing")
            return 0
        try:
            return int(raw)
        except ValueError:
            problems.append(f"{name} must be an integer")
            return 0

    def opt_int(name: str, default: int, lo: int, hi: int) -> int:
        raw = _env(name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            problems.append(f"{name} must be an integer")
            return default
        if not lo <= value <= hi:
            problems.append(f"{name} must be between {lo} and {hi}")
            return default
        return value

    api_id = need_int("API_ID")
    if api_id < 0 or (api_id == 0 and not any("API_ID" in p for p in problems)):
        problems.append("API_ID must be a positive integer")

    api_hash = _env("API_HASH")
    if not api_hash:
        problems.append("API_HASH is missing")
    elif not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        problems.append("API_HASH must be 32 hex characters")

    bot_token = _env("BOT_TOKEN")
    if not bot_token:
        problems.append("BOT_TOKEN is missing")
    elif not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", bot_token):
        problems.append("BOT_TOKEN does not look like a Bot API token (123456:ABC...)")

    session = _env("ASSISTANT_SESSION")
    if not session:
        problems.append("ASSISTANT_SESSION is missing (generate it with generate_session.py)")
    elif len(session) < 100 or re.search(r"\s", session):
        problems.append("ASSISTANT_SESSION looks invalid (expected one long string without spaces)")

    group_id = need_int("GROUP_ID")
    channel_id = need_int("STORAGE_CHANNEL_ID")
    for name, value in (("GROUP_ID", group_id), ("STORAGE_CHANNEL_ID", channel_id)):
        if value and value > 0:
            problems.append(f"{name} should be a negative chat id such as -100123456789")

    default_dir = Path("/var/data/downloads") if Path("/var/data").is_dir() else Path.cwd() / "downloads"
    download_dir = Path(_env("DOWNLOAD_DIR", str(default_dir))).expanduser().resolve()
    try:
        download_dir.mkdir(parents=True, exist_ok=True)
        probe = download_dir / ".write_test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        problems.append(f"DOWNLOAD_DIR {download_dir} is not writable: {exc}")

    scan_limit = opt_int("CACHE_SCAN_LIMIT", 500, 1, 50000)
    max_height = opt_int("MAX_VIDEO_HEIGHT", 480, 144, 1080)
    max_disk = opt_int("MAX_DISK_MB", 2000, 100, 1_000_000)

    log_level = (_env("LOG_LEVEL", "INFO") or "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        problems.append("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
        log_level = "INFO"

    if problems:
        print("Configuration errors:\n" + "\n".join(f" - {p}" for p in problems), file=sys.stderr)
        raise SystemExit(2)

    return Config(
        api_id=api_id, api_hash=api_hash or "", bot_token=bot_token or "",
        assistant_session=session or "", group_id=group_id, storage_channel_id=channel_id,
        cookies_path=_env("COOKIES_PATH"), download_dir=download_dir,
        cache_scan_limit=scan_limit, log_level=log_level,
        max_video_height=max_height, max_disk_mb=max_disk,
    )


class RedactFilter(logging.Filter):
    """Last line of defence: scrub secrets if they ever reach a log record."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging crash the bot
            return True
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, "[REDACTED]")
        record.msg, record.args = text, ()
        return True


def setup_logging(cfg: Config) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handler.addFilter(RedactFilter(cfg.secrets()))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(cfg.log_level)
    logging.getLogger("pyrogram").setLevel(logging.WARNING)


def pkg_version(*names: str) -> str:
    for name in names:
        try:
            return metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return "not installed"


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------
class MediaError(Exception):
    """Error whose message is safe to show to users."""


class CacheScanError(Exception):
    """The channel history could not be read (NOT the same as 'no match')."""


class CacheFetchError(Exception):
    """A cached message exists but its media could not be retrieved."""


def normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def fmt_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return "unknown"
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{sec:02d}" if hours else f"{minutes}:{sec:02d}"


def is_url(text: str) -> bool:
    return bool(re.match(r"https?://", text.strip(), re.I))


_YT_ID = re.compile(
    r"^https?://(?:[\w-]+\.)*(?:youtube\.com|youtu\.be)/"
    r"(?:watch\?(?:[^#\s]*&)?v=|shorts/|embed/|live/|)([\w-]{11})(?![\w-])",
    re.I,
)


def extract_youtube_id(url: str) -> Optional[str]:
    match = _YT_ID.match(url.strip())
    return match.group(1) if match else None


# --------------------------------------------------------------------------------------
# Caption metadata (the cache "schema")
# --------------------------------------------------------------------------------------
_FIELD = re.compile(r"^\s*(Title|Video ID|Type|Query)\s*:\s*(.*?)\s*$", re.I)


@dataclass
class CacheEntry:
    message_id: int
    kind: str
    title: str
    video_id: str
    query: str
    duration: Optional[int] = None


def _one_line(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


def build_caption(kind: str, title: str, video_id: str, query: str) -> str:
    caption = (
        f"Title: {_one_line(title, 200)}\n"
        f"Video ID: {_one_line(video_id, 32)}\n"
        f"Type: {kind}\n"
        f"Query: {_one_line(query, 200)}"
    )
    return caption[:1024]  # Telegram caption limit


def parse_caption(caption: Optional[str]) -> Optional[dict[str, str]]:
    """Return {'title','video_id','kind','query'} or None if the caption is not ours."""
    if not caption:
        return None
    found: dict[str, str] = {}
    for line in caption.splitlines():
        match = _FIELD.match(line)
        if match:
            found.setdefault(match.group(1).lower().replace(" ", "_"), match.group(2))
    kind = found.get("type", "").lower()
    video_id = found.get("video_id", "")
    if kind not in ("audio", "video") or not video_id:
        return None
    return {"title": found.get("title") or video_id, "video_id": video_id,
            "kind": kind, "query": found.get("query", "")}


class StorageCache:
    """Telegram storage channel used as the cache. History is read with the ASSISTANT client."""

    def __init__(self, cfg: Config, bot: Client, assistant: Client):
        self.cfg, self.bot, self.assistant = cfg, bot, assistant

    async def verify_access(self) -> None:
        channel = self.cfg.storage_channel_id
        try:
            chat = await self.assistant.get_chat(channel)
            async for _ in self.assistant.get_chat_history(channel, limit=1):
                break
        except RPCError as exc:
            raise SystemExit(
                f"Assistant account cannot read STORAGE_CHANNEL_ID {channel}: {type(exc).__name__}. "
                "Join the assistant account to the channel (as member or admin) and check the id."
            ) from exc
        log.info("Assistant can read storage channel %r", getattr(chat, "title", channel))

        try:
            member = await self.bot.get_chat_member(channel, "me")
            privileges = getattr(member, "privileges", None)
            owner = member.status == enums.ChatMemberStatus.OWNER
            if not owner and not (privileges and getattr(privileges, "can_post_messages", False)):
                log.warning("Bot is in the storage channel but lacks 'Post messages'; uploads will fail")
        except RPCError as exc:
            log.warning("Bot cannot inspect the storage channel (%s). Add it as an admin with "
                        "'Post messages' or caching will not work.", type(exc).__name__)

    async def find(self, kind: str, query: Optional[str], video_id: Optional[str]) -> Optional[CacheEntry]:
        wanted_query = normalize(query) if query else None
        scanned = 0
        try:
            async for msg in self.assistant.get_chat_history(
                self.cfg.storage_channel_id, limit=self.cfg.cache_scan_limit
            ):
                scanned += 1
                if getattr(msg, "empty", False):
                    continue
                media = msg.audio if kind == "audio" else msg.video
                if media is None:  # also guarantees audio/video are never mixed up
                    continue
                meta = parse_caption(msg.caption)
                if meta is None or meta["kind"] != kind:
                    continue
                id_hit = video_id is not None and meta["video_id"] == video_id
                query_hit = wanted_query is not None and (
                    normalize(meta["query"]) == wanted_query or normalize(meta["title"]) == wanted_query
                )
                if id_hit or query_hit:
                    log.info("Cache hit (%s) in message %s after scanning %d", kind, msg.id, scanned)
                    return CacheEntry(msg.id, kind, meta["title"], meta["video_id"], meta["query"],
                                      getattr(media, "duration", None))
        except RPCError as exc:
            raise CacheScanError(f"{type(exc).__name__}: {exc}") from exc
        except OSError as exc:
            raise CacheScanError(f"network error: {exc}") from exc
        log.info("Cache miss (%s) after scanning %d messages", kind, scanned)
        return None

    async def fetch(self, entry: CacheEntry, directory: Path) -> Path:
        try:
            msg = await self.assistant.get_messages(self.cfg.storage_channel_id, entry.message_id)
        except RPCError as exc:
            raise CacheFetchError(f"cannot load message {entry.message_id}: {type(exc).__name__}") from exc
        if msg is None or getattr(msg, "empty", False):
            raise CacheFetchError(f"message {entry.message_id} was deleted")
        media = msg.audio if entry.kind == "audio" else msg.video
        if media is None:
            raise CacheFetchError(f"message {entry.message_id} has no {entry.kind} media")
        suffix = Path(getattr(media, "file_name", "") or "").suffix or (".m4a" if entry.kind == "audio" else ".mp4")
        target = directory / f"{entry.video_id}{suffix}"
        directory.mkdir(parents=True, exist_ok=True)
        try:
            out = await self.assistant.download_media(msg, file_name=str(target))
        except (RPCError, OSError) as exc:
            raise CacheFetchError(f"download failed: {type(exc).__name__}") from exc
        if not out or not Path(out).is_file() or Path(out).stat().st_size == 0:
            raise CacheFetchError("download returned no file")
        return Path(out)

    async def upload(self, kind: str, path: Path, title: str, video_id: str, query: str,
                     duration: Optional[int], width: Optional[int], height: Optional[int]) -> int:
        caption = build_caption(kind, title, video_id, query)
        channel = self.cfg.storage_channel_id
        if kind == "audio":
            sent = await self.bot.send_audio(channel, str(path), caption=caption,
                                             title=_one_line(title, 64), duration=int(duration or 0))
        else:
            sent = await self.bot.send_video(channel, str(path), caption=caption,
                                             duration=int(duration or 0), width=int(width or 0),
                                             height=int(height or 0), supports_streaming=True)
        return sent.id


# --------------------------------------------------------------------------------------
# YouTube (yt-dlp)
# --------------------------------------------------------------------------------------
class _YtdlLogger:
    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        log.warning("yt-dlp: %s", str(msg)[:300])

    def error(self, msg: str) -> None:
        log.error("yt-dlp: %s", str(msg)[:300])


def prepare_cookie_file(path: Optional[str]) -> Optional[str]:
    """Copy the cookie file to a writable temp file (yt-dlp rewrites the jar; /etc/secrets is read-only)."""
    if not path:
        return None
    source = Path(path)
    if not source.is_file() or not os.access(source, os.R_OK):
        log.warning("COOKIES_PATH is set but the file is missing or unreadable; continuing without cookies")
        return None
    target = Path(tempfile.gettempdir()) / "yt_cookies.txt"
    shutil.copyfile(source, target)
    os.chmod(target, 0o600)
    head = target.read_text(errors="ignore")[:300]
    if "HTTP Cookie File" not in head:
        log.warning("Cookie file lacks the Netscape header line; export it in Netscape format")
    log.info("Using YouTube cookie file (contents not logged)")
    return str(target)


def map_ytdlp_error(exc: Exception) -> MediaError:
    text = str(exc)
    low = text.lower()
    log.warning("yt-dlp failure: %s", text.splitlines()[0][:300] if text else type(exc).__name__)
    if "sign in to confirm" in low or "not a bot" in low:
        return MediaError("YouTube asked for bot verification and blocked this request. "
                          "The owner needs to provide fresh cookies (see the troubleshooting guide); "
                          "cookies do not guarantee success.")
    if "private video" in low:
        return MediaError("That video is private.")
    if "age" in low and ("restricted" in low or "confirm your age" in low):
        return MediaError("That video is age-restricted and needs a logged-in cookie file.")
    if "unavailable" in low or "removed" in low or "terminated" in low or "not available" in low:
        return MediaError("That video is unavailable (deleted, region-locked or removed).")
    if "429" in low or "too many requests" in low:
        return MediaError("YouTube is rate-limiting this server. Try again later.")
    if "requested format is not available" in low:
        return MediaError("No compatible audio/video format was found for that video.")
    if "ffmpeg" in low:
        return MediaError("FFmpeg is missing or failed while processing the media.")
    return MediaError("Could not fetch that from YouTube (network or extractor error). Check the logs.")


@dataclass
class Downloaded:
    path: Path
    title: str
    video_id: str
    duration: Optional[int]
    width: Optional[int]
    height: Optional[int]


class YouTube:
    def __init__(self, cfg: Config, cookie_file: Optional[str]):
        self.cfg, self.cookie_file = cfg, cookie_file
        self._gate = asyncio.Semaphore(2)

    def _opts(self, **extra) -> dict:
        opts = {
            "quiet": True, "logger": _YtdlLogger(), "noplaylist": True,
            "socket_timeout": 30, "retries": 3, "fragment_retries": 3,
        }
        if self.cookie_file:
            opts["cookiefile"] = self.cookie_file
        opts.update(extra)
        return opts

    def find_local(self, kind: str, video_id: str) -> Optional[Path]:
        directory = self.cfg.download_dir / kind
        if not directory.is_dir():
            return None
        for candidate in directory.glob(f"{video_id}.*"):
            if candidate.stem == video_id and candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        return None

    def _search_sync(self, query: str) -> tuple[str, str]:
        try:
            with yt_dlp.YoutubeDL(self._opts(extract_flat=True, skip_download=True)) as ydl:
                info = ydl.extract_info(f"ytsearch1:{query}", download=False)
        except (DownloadError, ExtractorError) as exc:
            raise map_ytdlp_error(exc) from exc
        entries = [e for e in (info or {}).get("entries") or [] if e and e.get("id")]
        if not entries:
            raise MediaError("No YouTube results found for that search.")
        first = entries[0]
        return first["id"], first.get("title") or first["id"]

    def _download_sync(self, video_id: str, kind: str) -> Downloaded:
        directory = self.cfg.download_dir / kind
        directory.mkdir(parents=True, exist_ok=True)
        if kind == "audio":
            fmt = "bestaudio[ext=m4a]/bestaudio/best"
            extra = {}
        else:
            h = self.cfg.max_video_height
            fmt = (f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/bv*[height<={h}]+ba/"
                   f"b[height<={h}]/b")
            extra = {"merge_output_format": "mp4"}
        opts = self._opts(format=fmt, outtmpl=str(directory / "%(id)s.%(ext)s"), **extra)
        url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                requested = (info or {}).get("requested_downloads") or []
                filepath = requested[0].get("filepath") if requested else ydl.prepare_filename(info)
        except (DownloadError, ExtractorError) as exc:
            raise map_ytdlp_error(exc) from exc
        path = Path(filepath)
        if not path.is_file() or path.stat().st_size == 0:
            raise MediaError("Download finished but the media file is missing. Is FFmpeg installed?")
        return Downloaded(path, info.get("title") or video_id, video_id, info.get("duration"),
                          info.get("width"), info.get("height"))

    async def search(self, query: str) -> tuple[str, str]:
        async with self._gate:
            return await asyncio.to_thread(self._search_sync, query)

    async def download(self, video_id: str, kind: str) -> Downloaded:
        async with self._gate:
            return await asyncio.to_thread(self._download_sync, video_id, kind)


# --------------------------------------------------------------------------------------
# Voice (PyTgCalls adapter) - the ONLY place that touches PyTgCalls
# --------------------------------------------------------------------------------------
class VoicePlayer:
    """Thin adapter. It must be constructed and used inside the running event loop."""

    def __init__(self, assistant: Client, on_stream_end: Callable[[int], Awaitable[None]]):
        if pkg_version("py-tgcalls") == "not installed" and pkg_version("pytgcalls") != "not installed":
            raise SystemExit("The PyPI package 'pytgcalls' is an old, different project. "
                             "Install 'py-tgcalls' instead (pip uninstall pytgcalls; pip install py-tgcalls==3.0.0).")
        try:
            from pytgcalls import PyTgCalls
            from pytgcalls.types import MediaStream, StreamEnded
        except ImportError as exc:
            raise SystemExit(f"PyTgCalls import failed ({exc}). Installed py-tgcalls: "
                             f"{pkg_version('py-tgcalls')}. Check requirements.txt.") from exc
        self._MediaStream, self._StreamEnded = MediaStream, StreamEnded
        self._on_stream_end = on_stream_end
        self.calls = PyTgCalls(assistant)

        missing = [n for n in ("start", "play", "pause", "resume") if not callable(getattr(self.calls, n, None))]
        self._leave = getattr(self.calls, "leave_call", None) or getattr(self.calls, "leave_group_call", None)
        if self._leave is None:
            missing.append("leave_call")
        if not hasattr(MediaStream, "Flags") or not hasattr(self.calls, "on_update"):
            missing.append("MediaStream.Flags/on_update")
        if missing:
            raise SystemExit(f"Installed py-tgcalls {pkg_version('py-tgcalls')} lacks expected API: "
                             f"{', '.join(missing)}. Adjust VoicePlayer for that version.")
        self.calls.on_update()(self._dispatch)

    async def _dispatch(self, _client, update) -> None:
        if isinstance(update, self._StreamEnded):
            await self._on_stream_end(update.chat_id)

    async def start(self) -> None:
        await self.calls.start()

    def _stream(self, kind: str, path: Path):
        if kind == "audio":
            return self._MediaStream(str(path), video_flags=self._MediaStream.Flags.IGNORE)
        return self._MediaStream(str(path))

    async def play(self, chat_id: int, kind: str, path: Path) -> None:
        await asyncio.wait_for(self.calls.play(chat_id, self._stream(kind, path)), VOICE_TIMEOUT)

    async def pause(self, chat_id: int) -> None:
        await self.calls.pause(chat_id)

    async def resume(self, chat_id: int) -> None:
        await self.calls.resume(chat_id)

    async def leave(self, chat_id: int) -> None:
        try:
            await self._leave(chat_id)
        except Exception as exc:  # noqa: BLE001 - leaving when not in a call is not an error
            log.info("leave_call ignored: %s: %s", type(exc).__name__, str(exc)[:150])


def classify_voice_error(exc: Exception) -> tuple[str, bool]:
    """Return (user message, fatal_for_queue)."""
    low = f"{type(exc).__name__} {exc}".lower()
    if "noactivegroupcall" in low or "no active" in low or "groupcallnotfound" in low:
        return ("There is no active voice chat. Start one in the group and use /play again.", True)
    if isinstance(exc, asyncio.TimeoutError):
        return ("Joining the voice chat timed out.", False)
    return (f"Voice playback failed ({type(exc).__name__}: {str(exc)[:150]}).", False)


# --------------------------------------------------------------------------------------
# Queue / state
# --------------------------------------------------------------------------------------
@dataclass
class Track:
    kind: str
    title: str
    video_id: str
    query: str
    path: Path
    requested_by: str
    duration: Optional[int] = None
    source: str = "youtube"
    started_at: float = 0.0


@dataclass
class ChatState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    queue: deque = field(default_factory=deque)
    current: Optional[Track] = None
    paused: bool = False


def prune_downloads(root: Path, max_mb: int, protected: set[Path]) -> None:
    """Delete stale temp files, then oldest unprotected media until under max_mb. Runs in a thread."""
    now = time.time()
    files = [p for p in root.rglob("*") if p.is_file()]
    for p in files:
        if p.suffix in (".part", ".ytdl", ".temp") and now - p.stat().st_mtime > 3600:
            p.unlink(missing_ok=True)
    media = [p for p in root.rglob("*") if p.is_file() and p.suffix not in (".part", ".ytdl", ".temp")]
    total = sum(p.stat().st_size for p in media)
    limit = max_mb * 1024 * 1024
    for p in sorted(media, key=lambda f: f.stat().st_mtime):
        if total <= limit:
            break
        if p.resolve() in protected or now - p.stat().st_mtime < PRUNE_MIN_AGE:
            continue
        size = p.stat().st_size
        p.unlink(missing_ok=True)
        total -= size
        log.info("Pruned %s", p.name)


# --------------------------------------------------------------------------------------
# The bot
# --------------------------------------------------------------------------------------
class MusicBot:
    def __init__(self, cfg: Config):
        # NOTE: no Pyrogram/PyTgCalls objects are created here - only inside run(), in the loop.
        self.cfg = cfg
        self.state = ChatState()
        self._pending: set[Path] = set()
        self._tasks: set[asyncio.Task] = set()
        self.stop_event: Optional[asyncio.Event] = None
        self.bot: Client
        self.assistant: Client
        self.voice: VoicePlayer
        self.cache: StorageCache
        self.yt: YouTube

    # ---- lifecycle ----------------------------------------------------------------
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except NotImplementedError:  # non-POSIX
                pass

        log.info("Versions: python=%s PyrogramMod=%s py-tgcalls=%s ntgcalls=%s yt-dlp=%s",
                 sys.version.split()[0], pkg_version("PyrogramMod", "pyrogrammod"),
                 pkg_version("py-tgcalls"), pkg_version("ntgcalls"), pkg_version("yt-dlp"))
        if shutil.which("ffmpeg") is None:
            raise SystemExit("ffmpeg not found on PATH; install it (see Dockerfile).")
        if shutil.which("deno") is None:
            log.warning("deno not found; YouTube extraction may fail without a JS runtime")

        cfg = self.cfg
        # Clients are created HERE, inside the running loop (see module docstring).
        self.bot = Client("music_bot", api_id=cfg.api_id, api_hash=cfg.api_hash,
                          bot_token=cfg.bot_token, in_memory=True)
        self.assistant = Client("assistant", api_id=cfg.api_id, api_hash=cfg.api_hash,
                                session_string=cfg.assistant_session, in_memory=True)
        self.cache = StorageCache(cfg, self.bot, self.assistant)
        self.yt = YouTube(cfg, prepare_cookie_file(cfg.cookies_path))
        self._register_handlers()

        try:
            await self.bot.start()
            await self.assistant.start()
        except RPCError as exc:
            raise SystemExit(f"Telegram login failed ({type(exc).__name__}). Check BOT_TOKEN / "
                             f"ASSISTANT_SESSION / API_ID / API_HASH.") from exc

        try:
            await self.assistant.get_chat(cfg.group_id)  # also caches the peer for PyTgCalls
            await self.bot.get_chat(cfg.group_id)
        except RPCError as exc:
            raise SystemExit(f"GROUP_ID {cfg.group_id} is not accessible to both accounts "
                             f"({type(exc).__name__}). Add the bot and the assistant to the group.") from exc
        await self.cache.verify_access()

        self.voice = VoicePlayer(self.assistant, self.on_stream_end)
        await self.voice.start()
        log.info("Bot is up. Group=%s Storage=%s", cfg.group_id, cfg.storage_channel_id)

        await self.stop_event.wait()
        await self.shutdown()

    async def shutdown(self) -> None:
        log.info("Shutting down")
        try:
            async with self.state.lock:
                self.state.queue.clear()
                self.state.current = None
                await self.voice.leave(self.cfg.group_id)
        except Exception:  # noqa: BLE001
            log.exception("Error while leaving voice chat")
        for client in (self.assistant, self.bot):
            try:
                await client.stop()
            except Exception:  # noqa: BLE001
                log.exception("Error while stopping a client")

    # ---- handlers -----------------------------------------------------------------
    def _register_handlers(self) -> None:
        in_group = filters.chat(self.cfg.group_id)
        routes = {
            "play": self.cmd_play, "vplay": self.cmd_vplay, "queue": self.cmd_queue,
            "now": self.cmd_now, "skip": self.cmd_skip, "pause": self.cmd_pause,
            "resume": self.cmd_resume, "stop": self.cmd_stop,
        }
        for name, fn in routes.items():
            self.bot.add_handler(MessageHandler(self._guard(fn), filters.command(name) & in_group))

    def _guard(self, fn):
        async def wrapper(client: Client, message: Message):
            try:
                await fn(message)
            except Exception:  # noqa: BLE001 - report instead of silently dying
                log.exception("Unhandled error in %s", fn.__name__)
                await self._reply(message, "Something went wrong while handling that command. Check the logs.")
        return wrapper

    async def _reply(self, message: Message, text: str) -> Optional[Message]:
        try:
            return await message.reply_text(text, parse_mode=enums.ParseMode.DISABLED)
        except RPCError:
            log.exception("Failed to send reply")
            return None

    async def _edit(self, message: Optional[Message], text: str) -> None:
        if message is None:
            return
        try:
            await message.edit_text(text, parse_mode=enums.ParseMode.DISABLED)
        except RPCError as exc:
            log.debug("edit failed: %s", type(exc).__name__)

    async def _notify(self, text: str) -> None:
        try:
            await self.bot.send_message(self.cfg.group_id, text, parse_mode=enums.ParseMode.DISABLED)
        except RPCError:
            log.exception("Failed to notify group")

    @staticmethod
    def _requester(message: Message) -> str:
        user = message.from_user
        return (user.first_name or "someone") if user else "someone"

    # ---- /play, /vplay ------------------------------------------------------------
    async def cmd_play(self, message: Message) -> None:
        await self._play_command(message, "audio")

    async def cmd_vplay(self, message: Message) -> None:
        await self._play_command(message, "video")

    async def _play_command(self, message: Message, kind: str) -> None:
        query = " ".join(message.command[1:]).strip()
        if not query:
            await self._reply(message, f"Usage: /{'play' if kind == 'audio' else 'vplay'} <name or YouTube link>")
            return
        if len(query) > 200:
            await self._reply(message, "That query is too long (max 200 characters).")
            return
        status = await self._reply(message, "🔎 Looking for it in the cache…")

        async def progress(text: str) -> None:
            await self._edit(status, text)

        try:
            track = await self.prepare_track(kind, query, self._requester(message), progress)
        except MediaError as exc:
            await self._edit(status, f"❌ {exc}")
            return

        try:
            async with self.state.lock:
                self.state.queue.append(track)
                self._pending.discard(track.path)
                if self.state.current is None:
                    started, errors = await self._advance_locked()
                else:
                    started, errors = None, []
                position = next((i for i, t in enumerate(self.state.queue, 1) if t is track), None)
        finally:
            self._pending.discard(track.path)

        icon = "🎬" if kind == "video" else "🎵"
        if started is track:
            await self._edit(status, f"▶️ Now playing {icon} {track.title}")
        elif position is not None:
            await self._edit(status, f"➕ Queued at #{position}: {icon} {track.title}")
        else:
            await self._edit(status, "❌ " + (errors[-1] if errors else "Could not start playback."))

    async def prepare_track(self, kind: str, query: str, requester: str,
                            progress: Callable[[str], Awaitable[None]]) -> Track:
        link = is_url(query)
        video_id = extract_youtube_id(query) if link else None
        if link and not video_id:
            raise MediaError("Only single YouTube video links are supported (no playlists or other sites).")

        entry = await self._lookup(kind, None if link else query, video_id, progress)
        if entry is None and video_id is None:
            await progress("🔎 Not cached. Searching YouTube…")
            video_id, _ = await self.yt.search(query)
            entry = await self._lookup(kind, None, video_id, progress)

        if entry is not None:
            track = await self._from_cache(kind, entry, query, requester, progress)
            if track is not None:
                return track
            video_id = entry.video_id  # fallback policy: cached copy unusable -> download fresh

        if video_id is None:
            video_id, _ = await self.yt.search(query)
        await progress("⬇️ Downloading from YouTube…")
        dl = await self.yt.download(video_id, kind)
        self._pending.add(dl.path)

        await progress("☁️ Saving to the cache channel…")
        try:
            await self.cache.upload(kind, dl.path, dl.title, dl.video_id, query,
                                    dl.duration, dl.width, dl.height)
        except (RPCError, OSError) as exc:
            log.error("Upload to storage channel failed: %s: %s", type(exc).__name__, str(exc)[:200])
            await progress("⚠️ Couldn't save to the cache channel (check bot permissions); playing anyway…")
        return Track(kind, dl.title, dl.video_id, query, dl.path, requester, dl.duration, "youtube")

    async def _lookup(self, kind: str, query: Optional[str], video_id: Optional[str],
                      progress: Callable[[str], Awaitable[None]]) -> Optional[CacheEntry]:
        try:
            return await self.cache.find(kind, query, video_id)
        except CacheScanError as exc:
            log.error("Cache lookup FAILED (not a miss): %s", exc)
            await progress("⚠️ Couldn't read the cache channel; falling back to YouTube…")
            return None

    async def _from_cache(self, kind: str, entry: CacheEntry, query: str, requester: str,
                          progress: Callable[[str], Awaitable[None]]) -> Optional[Track]:
        path = self.yt.find_local(kind, entry.video_id)
        if path is None:
            await progress("📦 Found in cache. Fetching from Telegram…")
            try:
                path = await self.cache.fetch(entry, self.cfg.download_dir / kind)
            except CacheFetchError as exc:
                log.warning("Cached media unusable (%s); re-downloading", exc)
                await progress("⚠️ Cached file unavailable; downloading again…")
                return None
        self._pending.add(path)
        return Track(kind, entry.title, entry.video_id, query, path, requester, entry.duration, "cache")

    # ---- queue engine (call only with state.lock held) ----------------------------
    async def _advance_locked(self) -> tuple[Optional[Track], list[str]]:
        st, errors = self.state, []
        finished = st.current
        while st.queue:
            nxt: Track = st.queue.popleft()
            if not nxt.path.is_file():
                errors.append(f"Media file for {nxt.title} is missing.")
                log.error("Missing media file for %s", nxt.title)
                continue
            try:
                await self.voice.play(self.cfg.group_id, nxt.kind, nxt.path)
            except Exception as exc:  # noqa: BLE001
                message, fatal = classify_voice_error(exc)
                log.error("voice.play failed: %s: %s", type(exc).__name__, str(exc)[:300])
                errors.append(message)
                if fatal:
                    st.queue.clear()
                    break
                continue
            st.current, st.paused, nxt.started_at = nxt, False, time.monotonic()
            self._schedule_prune()
            return nxt, errors
        st.current, st.paused = None, False
        if finished is not None:
            await self.voice.leave(self.cfg.group_id)
        self._schedule_prune()
        return None, errors

    def _schedule_prune(self) -> None:
        protected = {t.path.resolve() for t in self.state.queue} | {p.resolve() for p in self._pending}
        if self.state.current:
            protected.add(self.state.current.path.resolve())
        task = asyncio.create_task(self._prune(protected))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _prune(self, protected: set[Path]) -> None:
        try:
            await asyncio.to_thread(prune_downloads, self.cfg.download_dir, self.cfg.max_disk_mb, protected)
        except OSError:
            log.exception("Pruning downloads failed")

    async def on_stream_end(self, chat_id: int) -> None:
        if chat_id != self.cfg.group_id:
            return
        async with self.state.lock:
            current = self.state.current
            if current is None:
                log.debug("Ignoring stream-end: nothing is current")
                return
            if time.monotonic() - current.started_at < MIN_PLAY_SECONDS:
                log.debug("Ignoring stale/duplicate stream-end")
                return
            started, errors = await self._advance_locked()
        for err in errors:
            await self._notify(f"⚠️ {err}")
        if started:
            await self._notify(f"▶️ Now playing {'🎬' if started.kind == 'video' else '🎵'} {started.title}")
        else:
            await self._notify("⏹ Queue finished.")

    # ---- other commands -----------------------------------------------------------
    async def cmd_queue(self, message: Message) -> None:
        st = self.state
        if st.current is None and not st.queue:
            await self._reply(message, "The queue is empty. Use /play or /vplay to add something.")
            return
        lines = []
        if st.current:
            c = st.current
            lines.append(f"▶️ Now: [{c.kind}] {c.title}" + (" (paused)" if st.paused else ""))
        upcoming = list(st.queue)
        if upcoming:
            lines.append("")
            lines.append(f"Up next ({len(upcoming)}):")
            for i, t in enumerate(upcoming[:QUEUE_PAGE], 1):
                lines.append(f"{i}. [{t.kind}] {t.title[:80]}")
            if len(upcoming) > QUEUE_PAGE:
                lines.append(f"…and {len(upcoming) - QUEUE_PAGE} more")
        await self._reply(message, "\n".join(lines))

    async def cmd_now(self, message: Message) -> None:
        c = self.state.current
        if c is None:
            await self._reply(message, "Nothing is playing right now.")
            return
        await self._reply(message, "\n".join([
            f"Title: {c.title}", f"Type: {c.kind}", f"Query: {c.query}",
            f"Duration: {fmt_duration(c.duration)}", f"Source: {c.source}",
            f"Requested by: {c.requested_by}", f"State: {'paused' if self.state.paused else 'playing'}",
        ]))

    async def cmd_skip(self, message: Message) -> None:
        async with self.state.lock:
            skipped = self.state.current
            if skipped is None:
                await self._reply(message, "Nothing is playing.")
                return
            started, errors = await self._advance_locked()
        if started:
            await self._reply(message, f"⏭ Skipped {skipped.title}. Now playing: {started.title}")
        else:
            extra = f" ({errors[-1]})" if errors else ""
            await self._reply(message, f"⏭ Skipped {skipped.title}. The queue is now empty.{extra}")

    async def cmd_pause(self, message: Message) -> None:
        async with self.state.lock:
            if self.state.current is None:
                await self._reply(message, "Nothing is playing.")
                return
            if self.state.paused:
                await self._reply(message, "Already paused. Use /resume.")
                return
            try:
                await self.voice.pause(self.cfg.group_id)
            except Exception as exc:  # noqa: BLE001
                log.exception("pause failed")
                await self._reply(message, f"Could not pause ({type(exc).__name__}).")
                return
            self.state.paused = True
        await self._reply(message, "⏸ Paused.")

    async def cmd_resume(self, message: Message) -> None:
        async with self.state.lock:
            if self.state.current is None:
                await self._reply(message, "Nothing is playing.")
                return
            if not self.state.paused:
                await self._reply(message, "Playback is not paused.")
                return
            try:
                await self.voice.resume(self.cfg.group_id)
            except Exception as exc:  # noqa: BLE001
                log.exception("resume failed")
                await self._reply(message, f"Could not resume ({type(exc).__name__}).")
                return
            self.state.paused = False
        await self._reply(message, "▶️ Resumed.")

    async def cmd_stop(self, message: Message) -> None:
        async with self.state.lock:
            st = self.state
            if st.current is None and not st.queue:
                await self._reply(message, "Nothing to stop.")
                return
            dropped = len(st.queue)
            st.queue.clear()
            st.current, st.paused = None, False
            await self.voice.leave(self.cfg.group_id)
        self._schedule_prune()
        await self._reply(message, f"⏹ Stopped and left the voice chat. Cleared {dropped} queued item(s).")


def main() -> None:
    cfg = load_config()
    setup_logging(cfg)
    try:
        # The one and only event loop of the process.
        asyncio.run(MusicBot(cfg).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
