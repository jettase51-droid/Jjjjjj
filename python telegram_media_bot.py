#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram media bot
- YouTube: 144p -> 2160p, video or MP3
- TikTok / Instagram: video download (source/no-watermark URL when yt-dlp provides it)
- Song recognition: ShazamIO from audio/video
- Spotify: URL -> MP3 via spotdl
- Song search: first 5 results -> inline menu -> download
- Audio/video message: recognize song -> ask whether to download

IMPORTANT:
1) Never hard-code your Telegram token. Use: export BOT_TOKEN="NEW_TOKEN"
2) ffmpeg + ffprobe must be installed.
3) Telegram cloud bots normally cannot send files larger than Telegram's bot upload limit.
   The bot checks the size before sending instead of crashing.
"""

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError
from yt_dlp import YoutubeDL
from shazamio import Shazam


# ---------------- CONFIG ----------------

BOT_TOKEN = "8920639912:AAFxvcFm6teG1ubFDph50zWeU5Pe-RygeOk"


BASE_DIR = Path.home() / "telegram_media_bot"
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Keep below the normal Telegram Bot API upload ceiling.
MAX_SEND_MB = 49
MAX_SEND_BYTES = MAX_SEND_MB * 1024 * 1024

# YouTube menu
YT_QUALITIES = {
    "144": "bestvideo[height<=144]+bestaudio/best[height<=144]",
    "240": "bestvideo[height<=240]+bestaudio/best[height<=240]",
    "360": "bestvideo[height<=360]+bestaudio/best[height<=360]",
    "480": "bestvideo[height<=480]+bestaudio/best[height<=480]",
    "720": "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "1440": "bestvideo[height<=1440]+bestaudio/best[height<=1440]",
    "2160": "bestvideo[height<=2160]+bestaudio/best[height<=2160]",
}

URL_RE = re.compile(r"^https?://", re.I)

# Per-user temporary state
USER_STATE = {}


# ---------------- HELPERS ----------------

def is_url(text: str) -> bool:
    return bool(URL_RE.match(text.strip()))


def is_youtube(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return "youtube.com" in host or "youtu.be" in host


def is_spotify(url: str) -> bool:
    return "spotify.com" in urlparse(url).netloc.lower()


def is_tiktok(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return "tiktok.com" in host or "vm.tiktok.com" in host


def is_instagram(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return "instagram.com" in host


def safe_name(name: str, max_len: int = 90) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_len] or "media"


def remove_file(path: Path):
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def newest_file(folder: Path):
    files = [p for p in folder.rglob("*") if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def check_tools():
    missing = []
    for x in ("ffmpeg", "ffprobe"):
        if shutil.which(x) is None:
            missing.append(x)
    return missing


def run_cmd(cmd, cwd=None):
    return subprocess.run(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


# ---------------- YT-DLP ----------------

def ytdlp_download(url: str, selector: str, out_dir: Path, mp3=False):
    out_dir.mkdir(parents=True, exist_ok=True)
    uid = uuid.uuid4().hex
    template = str(out_dir / f"{uid}_%(title).80s.%(ext)s")

    opts = {
        "outtmpl": template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        "socket_timeout": 30,
        "restrictfilenames": True,
        "overwrites": False,
    }

    if mp3:
        opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        })
    else:
        opts["format"] = selector
        opts["merge_output_format"] = "mp4"

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get("title") or "media"

    path = newest_file(out_dir)
    if not path:
        raise RuntimeError("Fayl yükləndi, amma nəticə faylı tapılmadı.")

    return path, title


def ytdlp_search(query: str, limit=5):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
    }

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(
            f"ytsearch{limit}:{query}",
            download=False
        )

    results = []
    for item in (info.get("entries") or [])[:limit]:
        if not item:
            continue
        results.append({
            "id": item.get("id"),
            "title": item.get("title") or "Naməlum",
            "url": item.get("webpage_url")
                   or (f"https://www.youtube.com/watch?v={item.get('id')}"
                       if item.get("id") else None),
            "duration": item.get("duration"),
        })
    return results


# ---------------- SHAZAM ----------------

async def recognize_song(file_path: Path):
    shazam = Shazam()
    result = await shazam.recognize_song(str(file_path))

    track = result.get("track")
    if not track:
        return None

    title = track.get("title") or ""
    artist = track.get("subtitle") or ""

    if not title:
        return None

    return {
        "title": title,
        "artist": artist,
        "full": f"{artist} - {title}" if artist else title,
    }


async def extract_audio_for_shazam(source: Path, folder: Path):
    output = folder / f"{uuid.uuid4().hex}.mp3"

    cmd = [
        "ffmpeg", "-y",
        "-i", str(source),
        "-vn",
        "-ac", "1",
        "-ar", "44100",
        "-b:a", "128k",
        str(output),
    ]

    p = await asyncio.to_thread(run_cmd, cmd)
    if p.returncode != 0 or not output.exists():
        raise RuntimeError("Audio çıxarmaq mümkün olmadı.")

    return output


# ---------------- DOWNLOAD FUNCTIONS ----------------

async def download_youtube(url: str, quality: str, mp3: bool):
    folder = Path(tempfile.mkdtemp(prefix="yt_", dir=DOWNLOAD_DIR))
    try:
        selector = YT_QUALITIES[quality]
        path, title = await asyncio.to_thread(
            ytdlp_download, url, selector, folder, mp3
        )
        return path, title
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


async def download_social(url: str):
    """
    yt-dlp chooses the best available source from TikTok/Instagram.
    If the extractor exposes a no-watermark source, that source is used.
    No guarantee is made for private/restricted posts.
    """
    folder = Path(tempfile.mkdtemp(prefix="social_", dir=DOWNLOAD_DIR))
    try:
        path, title = await asyncio.to_thread(
            ytdlp_download,
            url,
            "bestvideo+bestaudio/best",
            folder,
            False,
        )
        return path, title
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


async def download_spotify(url: str):
    """
    spotdl reads Spotify metadata and obtains the audio source it is able
    to access, then creates an MP3.
    """
    folder = Path(tempfile.mkdtemp(prefix="spotify_", dir=DOWNLOAD_DIR))

    cmd = [
        "spotdl",
        "--format", "mp3",
        "--bitrate", "192k",
        "--output", str(folder / "{artist} - {title}.{output-ext}"),
        url,
    ]

    p = await asyncio.to_thread(run_cmd, cmd)

    if p.returncode != 0:
        shutil.rmtree(folder, ignore_errors=True)
        err = (p.stderr or p.stdout or "spotdl xətası").strip()
        raise RuntimeError(err[-1800:])

    path = newest_file(folder)
    if not path:
        shutil.rmtree(folder, ignore_errors=True)
        raise RuntimeError("Spotify nəticə faylı tapılmadı.")

    return path, path.stem


async def download_song_from_youtube(url: str):
    folder = Path(tempfile.mkdtemp(prefix="song_", dir=DOWNLOAD_DIR))
    try:
        path, title = await asyncio.to_thread(
            ytdlp_download,
            url,
            "bestaudio/best",
            folder,
            True,
        )
        return path, title
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


# ---------------- TELEGRAM UI ----------------

def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 YouTube", callback_data="menu_yt"),
            InlineKeyboardButton("🎵 Spotify", callback_data="menu_sp"),
        ],
        [
            InlineKeyboardButton("🔎 Mahnı axtar", callback_data="menu_search"),
            InlineKeyboardButton("🎤 Mahnı analiz et", callback_data="menu_help_rec"),
        ],
    ])


def youtube_menu():
    rows = []
    for q in ("144", "240", "360", "480", "720", "1080", "1440", "2160"):
        rows.append([
            InlineKeyboardButton(f"🎬 {q}p", callback_data=f"yt_v_{q}"),
        ])
    rows.append([
        InlineKeyboardButton("🎵 MP3", callback_data="yt_mp3"),
    ])
    rows.append([
        InlineKeyboardButton("⬅️ Geri", callback_data="home"),
    ])
    return InlineKeyboardMarkup(rows)


def confirm_song_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬇️ Yüklə", callback_data="rec_download"),
            InlineKeyboardButton("❌ Xeyr", callback_data="rec_cancel"),
        ]
    ])


def search_menu(results):
    rows = []
    for i, item in enumerate(results):
        title = item["title"]
        if len(title) > 55:
            title = title[:52] + "..."
        rows.append([
            InlineKeyboardButton(
                f"{i+1}. {title}",
                callback_data=f"song_{i}"
            )
        ])
    rows.append([
        InlineKeyboardButton("⬅️ Geri", callback_data="home")
    ])
    return InlineKeyboardMarkup(rows)


async def safe_send_file(update: Update, path: Path, title: str):
    if not path.exists():
        raise RuntimeError("Fayl tapılmadı.")

    size = path.stat().st_size
    if size > MAX_SEND_BYTES:
        mb = size / 1024 / 1024
        await update.effective_message.reply_text(
            f"❌ Fayl çox böyükdür: {mb:.1f} MB.\n"
            f"Telegram bot üçün bu bot {MAX_SEND_MB} MB-dan böyük "
            f"faylı göndərməyə çalışmır."
        )
        return

    await update.effective_message.reply_chat_action(
        ChatAction.UPLOAD_DOCUMENT
    )

    suffix = path.suffix.lower()
    if suffix in (".mp3", ".m4a", ".wav", ".ogg", ".flac"):
        await update.effective_message.reply_audio(
            audio=path.open("rb"),
            title=title[:250],
        )
    else:
        await update.effective_message.reply_video(
            video=path.open("rb"),
            caption=title[:1024],
            supports_streaming=True,
        )


async def cleanup_path(path):
    try:
        parent = path.parent
        shutil.rmtree(parent, ignore_errors=True)
    except Exception:
        pass


# ---------------- COMMANDS ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    USER_STATE.pop(update.effective_user.id, None)

    text = (
        "🤖 <b>Media Bot</b>\n\n"
        "🎬 YouTube — 144p-dən 4K-a qədər + MP3\n"
        "📱 TikTok / Instagram — video\n"
        "🎤 Audio/video göndər — Shazam ilə mahnını tap\n"
        "🎵 Spotify linki — MP3\n"
        "🔎 Mahnı adı yaz — ilk 5 nəticə\n\n"
        "Linki və ya mahnı adını birbaşa da göndərə bilərsən."
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "İstifadə:\n\n"
        "• YouTube linki → keyfiyyət seç\n"
        "• TikTok/Instagram linki → video yüklənir\n"
        "• Spotify linki → MP3\n"
        "• Mahnı adı → ilk 5 nəticə\n"
        "• Audio/video → Shazam analiz edir\n\n"
        "YouTube üçün /start menyusundan istifadə et."
    )


# ---------------- TEXT ----------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return

    uid = update.effective_user.id

    # Search mode
    state = USER_STATE.get(uid, {})
    if state.get("mode") == "search":
        USER_STATE.pop(uid, None)

        await update.message.reply_text("🔎 İlk 5 nəticə axtarılır...")
        try:
            results = await asyncio.to_thread(ytdlp_search, text, 5)
            if not results:
                await update.message.reply_text("❌ Nəticə tapılmadı.")
                return

            USER_STATE[uid] = {
                "mode": "search_results",
                "results": results,
            }

            await update.message.reply_text(
                "🎵 İstədiyin mahnını seç:",
                reply_markup=search_menu(results),
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ Axtarış alınmadı:\n{str(e)[-1200:]}"
            )
        return

    # YouTube mode: URL arrived after choosing YouTube
    if state.get("mode") == "yt_wait":
        if is_youtube(text):
            USER_STATE[uid]["url"] = text
            await update.message.reply_text(
                "Keyfiyyəti seç:",
                reply_markup=youtube_menu(),
            )
            return

    if is_url(text):
        if is_youtube(text):
            USER_STATE[uid] = {
                "mode": "yt_wait",
                "url": text,
            }
            await update.message.reply_text(
                "🎬 YouTube linkidir.\nKeyfiyyəti seç:",
                reply_markup=youtube_menu(),
            )
            return

        if is_spotify(text):
            await update.message.reply_text("🎵 Spotify hazırlanır...")
            try:
                path, title = await download_spotify(text)
                await safe_send_file(update, path, title)
                await cleanup_path(path)
            except Exception as e:
                await update.message.reply_text(
                    f"❌ Spotify yükləmə alınmadı:\n{str(e)[-1600:]}"
                )
            return

        if is_tiktok(text) or is_instagram(text):
            await update.message.reply_text("📥 Video hazırlanır...")
            try:
                path, title = await download_social(text)
                await safe_send_file(update, path, title)
                await cleanup_path(path)
            except Exception as e:
                await update.message.reply_text(
                    f"❌ Video yüklənmədi:\n{str(e)[-1600:]}"
                )
            return

        await update.message.reply_text(
            "Bu link növünü tanımadım. YouTube, TikTok, Instagram və "
            "Spotify linkləri dəstəklənir."
        )
        return

    # Plain text = song search
    await update.message.reply_text(
        "🔎 Mahnı axtarılır..."
    )
    try:
        results = await asyncio.to_thread(ytdlp_search, text, 5)
        if not results:
            await update.message.reply_text("❌ Nəticə tapılmadı.")
            return

        USER_STATE[uid] = {
            "mode": "search_results",
            "results": results,
        }

        await update.message.reply_text(
            "🎵 İlk 5 nəticə:",
            reply_markup=search_menu(results),
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Axtarış xətası:\n{str(e)[-1200:]}"
        )


# ---------------- AUDIO / VIDEO RECOGNITION ----------------

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = update.effective_user.id

    file = None
    filename = None

    try:
        if msg.audio:
            file = await context.bot.get_file(msg.audio.file_id)
            filename = msg.audio.file_name or "audio.mp3"

        elif msg.voice:
            file = await context.bot.get_file(msg.voice.file_id)
            filename = "voice.ogg"

        elif msg.video:
            file = await context.bot.get_file(msg.video.file_id)
            filename = "video.mp4"

        elif msg.document:
            # Accept common audio/video documents.
            mime = (msg.document.mime_type or "").lower()
            if not (mime.startswith("audio/") or mime.startswith("video/")):
                return
            file = await context.bot.get_file(msg.document.file_id)
            filename = msg.document.file_name or "media"

        else:
            return

        folder = Path(tempfile.mkdtemp(prefix="recognize_", dir=DOWNLOAD_DIR))
        source = folder / safe_name(filename)

        await msg.reply_text("🎤 Mahnı analiz edilir...")
        await file.download_to_drive(str(source))

        audio = await extract_audio_for_shazam(source, folder)
        result = await recognize_song(audio)

        if not result:
            shutil.rmtree(folder, ignore_errors=True)
            await msg.reply_text(
                "❌ Mahnını dəqiq tapa bilmədim. Daha uzun və təmiz "
                "10–20 saniyəlik hissə göndər."
            )
            return

        USER_STATE[uid] = {
            "mode": "recognized",
            "folder": str(folder),
            "recognized": result,
        }

        await msg.reply_text(
            f"🎵 <b>Mahnını tapdım!</b>\n\n"
            f"👤 İfaçı: {result['artist'] or 'Naməlum'}\n"
            f"🎶 Mahnı: {result['title']}\n\n"
            f"MP3 kimi yükləyim?",
            parse_mode="HTML",
            reply_markup=confirm_song_menu(),
        )

    except Exception as e:
        try:
            shutil.rmtree(locals().get("folder", ""), ignore_errors=True)
        except Exception:
            pass

        await msg.reply_text(
            f"❌ Analiz zamanı xəta oldu:\n{str(e)[-1400:]}"
        )


# ---------------- CALLBACKS ----------------

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    uid = query.from_user.id
    data = query.data
    state = USER_STATE.get(uid, {})

    if data == "home":
        USER_STATE.pop(uid, None)
        await query.edit_message_text(
            "🤖 Menyu:",
            reply_markup=main_menu(),
        )
        return

    if data == "menu_yt":
        USER_STATE[uid] = {"mode": "yt_wait"}
        await query.edit_message_text(
            "🎬 YouTube linkini göndər:",
        )
        return

    if data == "menu_sp":
        await query.edit_message_text(
            "🎵 Spotify mahnı/playlist linkini göndər:"
        )
        USER_STATE[uid] = {"mode": "spotify_wait"}
        return

    if data == "menu_search":
        USER_STATE[uid] = {"mode": "search"}
        await query.edit_message_text(
            "🔎 Mahnının adını və ya ifaçı + mahnı adını yaz:"
        )
        return

    if data == "menu_help_rec":
        await query.edit_message_text(
            "🎤 Mahnının 10–20 saniyəlik səs/video hissəsini göndər.\n"
            "Shazam ilə analiz edib adını tapacağam."
        )
        return

    if data.startswith("yt_v_") or data == "yt_mp3":
        url = state.get("url")
        if not url:
            await query.edit_message_text(
         