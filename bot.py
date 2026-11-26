import asyncio
import http.cookiejar
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv
load_dotenv()  # reads .env from the current folder

import yt_dlp
from yt_dlp.networking import Request as YdlRequest
from yt_dlp.networking.impersonate import ImpersonateTarget
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# The token now comes from an environment variable instead of being written in the file.
# Start the bot with:  BOT_TOKEN="your-new-token" python bot.py
TOKEN = os.environ["BOT_TOKEN"]
# Comma-separated Telegram user IDs; empty = anyone can use the bot.
ALLOWED_USERS = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()}
# Telegram's standard Bot API caps uploads at 50 MB.
MAX_BYTES = 50 * 1024 * 1024
YTDLP_FORMAT = os.environ.get(
    "YTDLP_FORMAT", "bv*[filesize<40M]+ba[filesize<10M]/b[filesize<50M]/bv*+ba/b"
)

URL_RE = re.compile(r"https?://\S+")
log = logging.getLogger("ytdlp-bot")


class _YtdlpLogger:
    def debug(self, msg):
        log.debug("yt-dlp: %s", msg)

    def info(self, msg):
        log.info("yt-dlp: %s", msg)

    def warning(self, msg):
        log.warning("yt-dlp: %s", msg)

    def error(self, msg):
        log.error("yt-dlp: %s", msg)


def find_media_url(url: str):
    """Ask yt-dlp what the post points to, WITHOUT downloading or processing it.
    For a Reddit GIF post this returns the direct i.redd.it link."""
    opts = {
        "quiet": True,
        "logger": _YtdlpLogger(),
        "cookiesfrombrowser": ("brave",),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        # process=False stops yt-dlp from following the link into the extractor that fails
        info = ydl.extract_info(url, download=False, process=False)
    return info.get("url") if info else None


REDDIT_HOST_RE = re.compile(r"^(?:[\w-]+\.)?reddit\.com$|^redd\.it$")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def is_reddit(url: str) -> bool:
    return bool(REDDIT_HOST_RE.match(urlparse(url).hostname or ""))


def reddit_post(url: str):
    """Fetch a Reddit post's JSON the way yt-dlp's extractor does (impersonated browser
    request + logged-in cookies). Returns (clean_post_url, post_data)."""
    imp = {"impersonate": ImpersonateTarget.from_str("chrome")}
    opts = {"cookiesfrombrowser": ("brave",), "quiet": True, "logger": _YtdlpLogger()}
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.cookiejar.set_cookie(http.cookiejar.Cookie(
            0, "over18", "1", None, False, ".reddit.com", True, True,
            "/", True, False, None, False, None, None, {}))
        ydl.urlopen(YdlRequest("https://old.reddit.com/", extensions=imp)).read()
        if "/comments/" not in url:
            # Short share links (/s/...) redirect to the real post URL
            resp = ydl.urlopen(YdlRequest(url, extensions=imp))
            url = resp.url
            resp.close()
        post_url = url.split("?")[0].rstrip("/")
        resp = ydl.urlopen(YdlRequest(post_url + "/.json", query={"raw_json": 1}, extensions=imp))
        data = json.load(resp)[0]["data"]["children"][0]["data"]
    return post_url, data


def reddit_image_urls(data: dict) -> list:
    """Direct image/GIF links for gallery, image and GIF posts (empty for videos)."""
    meta = data.get("media_metadata") or {}
    order = [i["media_id"] for i in (data.get("gallery_data") or {}).get("items", [])] or list(meta)
    urls = []
    for media_id in order:
        s = (meta.get(media_id) or {}).get("s") or {}
        link = s.get("gif") or s.get("mp4") or s.get("u")
        if link:
            urls.append(link)
    if not urls:
        link = data.get("url_overridden_by_dest") or ""
        if (urlparse(link).hostname or "").endswith("redd.it"):
            urls.append(link)
    return urls


REDGIFS_THUMB_RE = re.compile(r"^https?://i\.redgifs\.com/i/(\w+)")


def external_media_url(data: dict):
    """The off-Reddit link a post points to (RedGifs, Imgur, YouTube...), or None.
    Giving yt-dlp this link avoids its Reddit extractor, which Reddit often blocks."""
    link = data.get("url_overridden_by_dest") or ""
    if not link.startswith("http") or is_reddit(link) or "redd.it" in (urlparse(link).hostname or ""):
        return None
    # i.redgifs.com/i/<id>.jpg is only a thumbnail; the watch page has the real media
    if m := REDGIFS_THUMB_RE.match(link):
        return f"https://www.redgifs.com/watch/{m.group(1)}"
    return link


def download_direct(url: str, folder: Path) -> Path:
    """Download a plain file (like a GIF) straight from its URL, the same way curl does."""
    # Some sites reject requests that have no browser-like User-Agent
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    # Use the last part of the URL as the file name, e.g. "mabk16afnmyh1.gif"
    name = Path(urlparse(url).path).name or "download.gif"
    target = folder / name
    with urlopen(req, timeout=60) as response, target.open("wb") as f:
        shutil.copyfileobj(response, f)  # copy the data to disk in chunks
    return target


def download_ytdlp(url: str, folder: Path) -> Path:
    opts = {
        "format": YTDLP_FORMAT,
        "outtmpl": str(folder / "%(title).100B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "verbose": True,
        "logger": _YtdlpLogger(),
        "merge_output_format": "mp4",
        "restrictfilenames": True,
        "cookiesfrombrowser": ("brave",),
        "js_runtimes": {"node": {}},
    }

    # Step 1: try yt-dlp first (works for normal videos)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError:
        # yt-dlp failed (e.g. the post is a GIF, not a video).
        # Step 2: try to get the direct file link and download it ourselves.
        media_url = find_media_url(url)
        if not media_url or not media_url.startswith("http"):
            raise  # nothing to fall back on, so report the original error
        log.info("Falling back to direct download: %s", media_url)
        return download_direct(media_url, folder)

    files = [f for f in folder.iterdir() if f.is_file()]
    if not files:
        # yt-dlp did not fail, but it also saved nothing. Try the direct link too.
        media_url = find_media_url(url)
        if media_url and media_url.startswith("http"):
            log.info("No file from yt-dlp, falling back to direct download: %s", media_url)
            return download_direct(media_url, folder)
        raise RuntimeError("yt-dlp produced no file")
    return max(files, key=lambda f: f.stat().st_size)


def download(url: str, folder: Path) -> list:
    """Return the downloaded files. Reddit image/GIF/gallery posts are fetched directly;
    everything else (including Reddit videos) goes through yt-dlp."""
    if is_reddit(url):
        try:
            post_url, data = reddit_post(url)
        except Exception:
            log.exception("Reddit JSON lookup failed, falling back to yt-dlp")
        else:
            if not data.get("is_video"):
                links = reddit_image_urls(data)
                if links:
                    log.info("Reddit direct download: %s", links)
                    return [download_direct(link, folder) for link in links]
            url = external_media_url(data) or post_url
    return [download_ytdlp(url, folder)]


def compress_to_hevc(path: Path) -> Path:
    """Re-encode to H.265 10-bit MP4 (CRF 28, fast preset), keeping resolution and the
    source frame timing. The original file is deleted; only the new file is returned."""
    target = path.with_name(path.stem + "_hevc.mp4")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(path),
        "-map", "0:v:0", "-map", "0:a:0?",
        # H.265 needs even dimensions; this trims at most 1 px when a side is odd
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-fps_mode", "passthrough",
        "-c:v", "libx265", "-preset", "fast", "-crf", "28",
        "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(target),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not target.exists():
        target.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed: {result.stderr[-300:]}")
    path.unlink(missing_ok=True)
    return target


def shrink(path: Path) -> Path:
    """Run `media-organizer shrink` on the file and return where it is now. Images become
    WebP; videos, animations and conversions that don't help keep the original file."""
    try:
        result = subprocess.run(["media-organizer", "shrink", str(path)],
                                capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("shrink failed for %s: %s", path.name, e)
        return path
    # Output looks like: "ok /dir/a.jpg -> a.webp (msg)" or "skipped /dir/a.mp4 (msg)"
    line = result.stdout.strip()
    log.info("shrink: %s", line or result.stderr.strip()[-300:])
    if line.startswith("ok ") and " -> " in line:
        new_name = line.split(" -> ", 1)[1].rsplit(" (", 1)[0]
        new_path = path.with_name(new_name)
        if new_path.is_file():
            return new_path
    return path


async def send_file(msg, path: Path):
    kwargs = {"filename": path.name, "read_timeout": 600, "write_timeout": 600}
    ext = path.suffix.lower()
    with path.open("rb") as f:
        if ext == ".gif":
            # Telegram plays GIFs inline when they are sent as an "animation"
            await msg.reply_animation(animation=f, **kwargs)
        elif ext in IMAGE_EXTS and path.stat().st_size <= 10 * 1024 * 1024:
            await msg.reply_photo(photo=f, **kwargs)
        elif ext in IMAGE_EXTS:
            await msg.reply_document(document=f, **kwargs)
        else:
            await msg.reply_video(video=f, supports_streaming=True, **kwargs)


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if ALLOWED_USERS and update.effective_user.id not in ALLOWED_USERS:
        await msg.reply_text("Not authorized.")
        return
    urls = URL_RE.findall(msg.text or "")
    if not urls:
        await msg.reply_text("Send me a URL to download.")
        return
    for url in urls:
        status = await msg.reply_text(f"Downloading {url} ...")
        folder = Path(tempfile.mkdtemp(prefix="ytdlp-"))
        try:
            paths = await asyncio.to_thread(download, url, folder)
            await status.edit_text("Uploading ...")
            for path in paths:
                path = await asyncio.to_thread(shrink, path)
                if path.stat().st_size > MAX_BYTES:
                    await status.edit_text(f"{path.name} is over 50 MB, converting to HEVC ...")
                    path = await asyncio.to_thread(compress_to_hevc, path)
                    await status.edit_text("Uploading ...")
                if path.stat().st_size > MAX_BYTES:
                    await msg.reply_text(f"{path.name} is still over Telegram's 50 MB limit.")
                    continue
                await send_file(msg, path)
            await status.delete()
        except Exception as e:
            log.exception("failed")
            await status.edit_text(f"Failed: {str(e)[:300]}")
        finally:
            shutil.rmtree(folder, ignore_errors=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Send me a URL and I'll download it with yt-dlp and send you the file."
    )


def main():
    logging.basicConfig(
        level=logging.INFO,
        handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")],
    )
    log.setLevel(logging.DEBUG)
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.run_polling()


if __name__ == "__main__":
    main()