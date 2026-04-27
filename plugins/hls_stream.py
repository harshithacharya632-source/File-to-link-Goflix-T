# ╔══════════════════════════════════════════════════════════════╗
# ║   GoFlix - HLS Multi-Audio & Subtitle Streaming              ║
# ║   File: plugins/hls_stream.py                                ║
# ║                                                              ║
# ║   DROP THIS FILE INTO:  plugins/                             ║
# ║                                                              ║
# ║   Requirements:                                              ║
# ║     pip install aiofiles aiohttp                             ║
# ║     sudo apt install ffmpeg   (or add ffmpeg buildpack)      ║
# ╚══════════════════════════════════════════════════════════════╝

import os
import json
import asyncio
import subprocess
import logging
import time
import shutil
from pathlib import Path
from pyrogram import Client, filters
from pyrogram.types import Message
from aiohttp import web

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════
#  CONFIG  — edit these to match your info.py values
# ══════════════════════════════════════════════════════════
HLS_BASE_DIR  = "/tmp/goflix_hls"          # Where HLS segments are stored
HLS_BASE_URL  = "http://localhost:8080"    # Your web server base URL (from info.py URL)
HLS_PORT      = 8081                       # Port for HLS segment server
SEGMENT_TIME  = 4                          # Seconds per HLS segment
CACHE_HOURS   = 2                          # Hours to keep HLS files before cleanup

os.makedirs(HLS_BASE_DIR, exist_ok=True)


# ══════════════════════════════════════════════════════════
#  LANGUAGE MAPS
# ══════════════════════════════════════════════════════════
LANG_NAMES = {
    "eng":"English","en":"English","hin":"Hindi","hi":"Hindi",
    "tam":"Tamil","ta":"Tamil","tel":"Telugu","te":"Telugu",
    "mal":"Malayalam","ml":"Malayalam","kan":"Kannada","kn":"Kannada",
    "ben":"Bengali","bn":"Bengali","mar":"Marathi","mr":"Marathi",
    "pun":"Punjabi","pa":"Punjabi","urd":"Urdu","ur":"Urdu",
    "ara":"Arabic","ar":"Arabic","fre":"French","fr":"French",
    "spa":"Spanish","es":"Spanish","ger":"German","de":"German",
    "jpn":"Japanese","ja":"Japanese","kor":"Korean","ko":"Korean",
    "chi":"Chinese","zh":"Chinese","zho":"Chinese","rus":"Russian",
    "ru":"Russian","por":"Portuguese","pt":"Portuguese",
    "ita":"Italian","it":"Italian","tur":"Turkish","tr":"Turkish",
    "und":"Unknown",
}
LANG_FLAGS = {
    "eng":"🇬🇧","en":"🇬🇧","hin":"🇮🇳","hi":"🇮🇳","tam":"🇮🇳","ta":"🇮🇳",
    "tel":"🇮🇳","te":"🇮🇳","mal":"🇮🇳","ml":"🇮🇳","kan":"🇮🇳","kn":"🇮🇳",
    "ben":"🇮🇳","bn":"🇮🇳","ara":"🇸🇦","ar":"🇸🇦","fre":"🇫🇷","fr":"🇫🇷",
    "spa":"🇪🇸","es":"🇪🇸","ger":"🇩🇪","de":"🇩🇪","jpn":"🇯🇵","ja":"🇯🇵",
    "kor":"🇰🇷","ko":"🇰🇷","chi":"🇨🇳","zh":"🇨🇳","rus":"🇷🇺","ru":"🇷🇺",
    "por":"🇵🇹","pt":"🇵🇹","ita":"🇮🇹","it":"🇮🇹","tur":"🇹🇷","tr":"🇹🇷",
}


# ══════════════════════════════════════════════════════════
#  FFPROBE — Get all audio & subtitle tracks
# ══════════════════════════════════════════════════════════
def probe_tracks(file_path: str) -> dict:
    """Extract audio and subtitle track info from file using ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", file_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        data   = json.loads(result.stdout)
    except Exception as e:
        logger.error(f"[ffprobe] {e}")
        return {"audio": [], "subtitles": []}

    audio     = []
    subtitles = []

    for s in data.get("streams", []):
        kind  = s.get("codec_type", "")
        idx   = s.get("index", 0)
        tags  = s.get("tags", {})
        lang  = (tags.get("language") or tags.get("LANGUAGE") or "und").lower().strip()
        title = tags.get("title") or tags.get("TITLE") or ""
        codec = s.get("codec_name", "").upper()
        label = title if title else LANG_NAMES.get(lang, lang.upper())
        flag  = LANG_FLAGS.get(lang, "🎵" if kind == "audio" else "💬")

        if kind == "audio":
            ch   = s.get("channels", 2)
            ch_s = {1:"Mono",2:"Stereo",6:"5.1",8:"7.1"}.get(ch, f"{ch}ch")
            audio.append({
                "stream_index": idx,      # ffmpeg stream index (e.g. 0:1)
                "audio_index":  len(audio), # 0,1,2... for ffmpeg -map
                "label": label, "lang": lang,
                "codec": codec, "channels": ch_s, "flag": flag,
            })
        elif kind == "subtitle":
            subtitles.append({
                "stream_index":    idx,
                "subtitle_index":  len(subtitles),
                "label": label, "lang": lang,
                "codec": codec, "flag": flag,
            })

    return {"audio": audio, "subtitles": subtitles}


# ══════════════════════════════════════════════════════════
#  FFMPEG — Transcode to HLS with all audio + subtitle tracks
# ══════════════════════════════════════════════════════════
async def transcode_to_hls(file_path: str, session_id: str, tracks: dict) -> dict:
    """
    Transcode MKV/MP4 to HLS with:
      - All audio tracks as separate HLS renditions
      - All subtitles extracted as .vtt files
      - Master playlist linking everything together

    Returns dict with paths and URLs for the master playlist and subtitle VTTs.
    """
    out_dir = os.path.join(HLS_BASE_DIR, session_id)
    os.makedirs(out_dir, exist_ok=True)

    audio_tracks    = tracks["audio"]
    subtitle_tracks = tracks["subtitles"]

    # ── Step 1: Extract subtitles as VTT files (fast, no video processing) ──
    subtitle_urls = []
    for sub in subtitle_tracks:
        vtt_name = f"sub_{sub['subtitle_index']}_{sub['lang']}.vtt"
        vtt_path = os.path.join(out_dir, vtt_name)

        # ffmpeg subtitle extraction command
        sub_cmd = [
            "ffmpeg", "-y", "-i", file_path,
            "-map", f"0:{sub['stream_index']}",
            "-f", "webvtt",
            vtt_path
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *sub_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            await asyncio.wait_for(proc.wait(), timeout=60)
            if os.path.exists(vtt_path) and os.path.getsize(vtt_path) > 0:
                subtitle_urls.append({
                    "label":    sub["label"],
                    "lang":     sub["lang"],
                    "flag":     sub["flag"],
                    "url":      f"{HLS_BASE_URL}/hls/{session_id}/{vtt_name}",
                    "filename": vtt_name,
                })
        except Exception as e:
            logger.warning(f"[subtitle extract] {sub['label']}: {e}")

    # ── Step 2: Build FFmpeg HLS command for video + all audio tracks ──
    # We create one HLS stream per audio track
    audio_playlists = []

    if not audio_tracks:
        # Single audio — simple transcode
        audio_tracks = [{"stream_index": None, "audio_index": 0,
                         "label": "Default", "lang": "und",
                         "flag": "🎵", "codec": "", "channels": "Stereo"}]

    for aud in audio_tracks:
        aud_idx   = aud["audio_index"]
        lang      = aud["lang"]
        label     = aud["label"]
        playlist  = f"audio_{aud_idx}_{lang}.m3u8"
        seg_name  = f"audio_{aud_idx}_{lang}_%04d.ts"

        # Build ffmpeg command for this audio track
        cmd = [
            "ffmpeg", "-y",
            "-i", file_path,
            # Video stream
            "-map", "0:v:0",
            # Specific audio track
            "-map", f"0:a:{aud_idx}",
            # Video codec — copy if possible
            "-c:v", "copy",
            # Audio codec — AAC for HLS compatibility
            "-c:a", "aac",
            "-b:a", "192k",
            "-ac", "2",                    # Stereo output
            # HLS settings
            "-f", "hls",
            "-hls_time", str(SEGMENT_TIME),
            "-hls_list_size", "0",         # Keep all segments
            "-hls_segment_filename", os.path.join(out_dir, seg_name),
            "-hls_flags", "independent_segments",
            os.path.join(out_dir, playlist),
        ]

        audio_playlists.append({
            "label":    label,
            "lang":     lang,
            "flag":     aud["flag"],
            "channels": aud["channels"],
            "playlist": playlist,
            "url":      f"{HLS_BASE_URL}/hls/{session_id}/{playlist}",
            "cmd":      cmd,
        })

    # ── Step 3: Start all FFmpeg transcoding jobs in parallel ──
    logger.info(f"[GoFlix HLS] Starting transcode: {len(audio_playlists)} audio tracks")
    procs = []
    for ap in audio_playlists:
        proc = await asyncio.create_subprocess_exec(
            *ap["cmd"],
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE
        )
        procs.append((ap, proc))
        # Small delay between starts to avoid I/O spike
        await asyncio.sleep(0.5)

    # ── Step 4: Wait for first segments to appear (so player can start) ──
    # We don't wait for full transcode — HLS streams as it goes
    for ap, proc in procs:
        playlist_path = os.path.join(out_dir, ap["playlist"])
        for _ in range(60):  # Wait up to 30 seconds
            if os.path.exists(playlist_path) and os.path.getsize(playlist_path) > 100:
                break
            await asyncio.sleep(0.5)
        else:
            logger.warning(f"[GoFlix HLS] Playlist not ready: {ap['playlist']}")

    # ── Step 5: Build master HLS playlist (links all audio renditions) ──
    master_lines = ["#EXTM3U", "#EXT-X-VERSION:3", ""]

    # Add subtitle tracks to master
    for sub_url in subtitle_urls:
        master_lines.append(
            f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",'
            f'NAME="{sub_url["label"]}",LANGUAGE="{sub_url["lang"]}",'
            f'URI="{sub_url["url"]}",DEFAULT=NO,AUTOSELECT=NO'
        )

    if subtitle_urls:
        master_lines.append("")

    # Add audio renditions
    for i, ap in enumerate(audio_playlists):
        default = "YES" if i == 0 else "NO"
        master_lines.append(
            f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",'
            f'NAME="{ap["label"]}",LANGUAGE="{ap["lang"]}",'
            f'URI="{ap["url"]}",DEFAULT={default},AUTOSELECT={default}'
        )

    master_lines.append("")

    # Video stream entry — references audio group
    sub_ref = ',SUBTITLES="subs"' if subtitle_urls else ""
    master_lines.append(
        f'#EXT-X-STREAM-INF:BANDWIDTH=2000000,AUDIO="audio"{sub_ref}'
    )
    # Point to first audio playlist (video is embedded in each)
    master_lines.append(audio_playlists[0]["url"])

    master_content  = "\n".join(master_lines)
    master_filename = "master.m3u8"
    master_path     = os.path.join(out_dir, master_filename)

    with open(master_path, "w") as f:
        f.write(master_content)

    master_url = f"{HLS_BASE_URL}/hls/{session_id}/{master_filename}"
    logger.info(f"[GoFlix HLS] Master playlist ready: {master_url}")

    return {
        "master_url":    master_url,
        "session_id":    session_id,
        "audio_tracks":  audio_playlists,
        "subtitle_urls": subtitle_urls,
        "out_dir":       out_dir,
    }


# ══════════════════════════════════════════════════════════
#  AIOHTTP ROUTES — Serve HLS files
# ══════════════════════════════════════════════════════════
# Add these routes to your existing aiohttp app in web/__init__.py or server.py
#
# from plugins.hls_stream import hls_file_handler, add_hls_routes
# add_hls_routes(app)  # call this when setting up your aiohttp app

async def hls_file_handler(request: web.Request) -> web.Response:
    """Serve HLS segments, playlists and VTT subtitle files."""
    session_id = request.match_info["session_id"]
    filename   = request.match_info["filename"]

    # Security: prevent path traversal
    if ".." in session_id or ".." in filename:
        return web.Response(status=403)

    file_path = os.path.join(HLS_BASE_DIR, session_id, filename)

    if not os.path.exists(file_path):
        return web.Response(status=404, text="File not found")

    # Content type mapping
    ext = Path(filename).suffix.lower()
    content_types = {
        ".m3u8": "application/vnd.apple.mpegurl",
        ".ts":   "video/mp2t",
        ".vtt":  "text/vtt",
        ".webvtt": "text/vtt",
    }
    content_type = content_types.get(ext, "application/octet-stream")

    headers = {
        "Access-Control-Allow-Origin":  "*",
        "Access-Control-Allow-Headers": "*",
        "Cache-Control": "no-cache" if ext == ".m3u8" else "max-age=3600",
    }

    return web.FileResponse(file_path, headers=headers)


def add_hls_routes(app: web.Application):
    """
    Call this in your web server setup to add HLS routes.

    Example in your web/__init__.py or server.py:
        from plugins.hls_stream import add_hls_routes
        add_hls_routes(app)
    """
    app.router.add_get("/hls/{session_id}/{filename}", hls_file_handler)
    app.router.add_options("/hls/{session_id}/{filename}", hls_file_handler)
    logger.info("[GoFlix HLS] Routes registered: /hls/{session_id}/{filename}")


# ══════════════════════════════════════════════════════════
#  CLEANUP — Remove old HLS sessions
# ══════════════════════════════════════════════════════════
async def cleanup_old_sessions():
    """Background task — delete HLS folders older than CACHE_HOURS."""
    while True:
        await asyncio.sleep(3600)  # Run every hour
        try:
            now = time.time()
            for folder in os.listdir(HLS_BASE_DIR):
                folder_path = os.path.join(HLS_BASE_DIR, folder)
                if os.path.isdir(folder_path):
                    age_hours = (now - os.path.getmtime(folder_path)) / 3600
                    if age_hours > CACHE_HOURS:
                        shutil.rmtree(folder_path, ignore_errors=True)
                        logger.info(f"[GoFlix HLS] Cleaned session: {folder}")
        except Exception as e:
            logger.error(f"[GoFlix HLS] Cleanup error: {e}")


# ══════════════════════════════════════════════════════════
#  MAIN FUNCTION — Call from your existing stream plugin
# ══════════════════════════════════════════════════════════
async def prepare_hls_stream(file_path: str, session_id: str) -> dict:
    """
    Main entry point. Call this from your existing plugin.

    Args:
        file_path:  Local path to MKV/MP4 file (after download)
        session_id: Unique ID for this stream (e.g. file_id or message_id)

    Returns:
        {
            "master_url":    "http://yourdomain.com/hls/abc123/master.m3u8",
            "audio_tracks":  [...],
            "subtitle_urls": [...],
            "audio_html":    "<div>...</div>",    # Ready HTML for template
            "subtitle_html": "<div>...</div>",    # Ready HTML for template
            "audio_json":    "[...]",              # JSON string for JS
            "subtitle_json": "[...]",              # JSON string for JS
        }

    Usage in your plugin:
        from plugins.hls_stream import prepare_hls_stream

        result = await prepare_hls_stream(temp_file_path, str(message.id))
        master_url    = result["master_url"]
        audio_html    = result["audio_html"]
        subtitle_html = result["subtitle_html"]
        audio_json    = result["audio_json"]
        subtitle_json = result["subtitle_json"]
        # Put these into your HTML template
    """
    # Get tracks
    loop   = asyncio.get_event_loop()
    tracks = await loop.run_in_executor(None, probe_tracks, file_path)

    logger.info(
        f"[GoFlix HLS] Found {len(tracks['audio'])} audio, "
        f"{len(tracks['subtitles'])} subtitle tracks"
    )

    # Transcode to HLS
    result = await transcode_to_hls(file_path, session_id, tracks)

    # Build HTML for template
    audio_html    = _build_audio_html(result["audio_tracks"])
    subtitle_html = _build_subtitle_html(result["subtitle_urls"])
    audio_json    = json.dumps([{
        "label":    a["label"],
        "lang":     a["lang"],
        "flag":     a["flag"],
        "url":      a["url"],
    } for a in result["audio_tracks"]], ensure_ascii=False)
    subtitle_json = json.dumps(result["subtitle_urls"], ensure_ascii=False)

    result["audio_html"]    = audio_html
    result["subtitle_html"] = subtitle_html
    result["audio_json"]    = audio_json
    result["subtitle_json"] = subtitle_json

    return result


# ── Internal HTML builders ─────────────────────────────────
def _build_audio_html(audio_tracks: list) -> str:
    if not audio_tracks:
        return '<div class="mo-empty"><i class="fa fa-headphones"></i><br>No audio tracks</div>'
    html = ""
    for i, t in enumerate(audio_tracks):
        active = "active" if i == 0 else ""
        html += f"""<div class="mo-item {active}">
  <div class="mo-item-icon">{t['flag']}</div>
  <div class="mo-item-info">
    <div class="mo-item-name">{t['label']}</div>
    <div class="mo-item-sub">HLS Audio Track</div>
  </div>
  <i class="fa fa-check mo-item-check"></i>
</div>"""
    return html


def _build_subtitle_html(subtitle_urls: list) -> str:
    html = """<div class="mo-item active">
  <div class="mo-item-icon">🚫</div>
  <div class="mo-item-info">
    <div class="mo-item-name">Off</div>
    <div class="mo-item-sub">No subtitles</div>
  </div>
  <i class="fa fa-check mo-item-check"></i>
</div>"""
    for s in subtitle_urls:
        html += f"""<div class="mo-item">
  <div class="mo-item-icon">{s['flag']}</div>
  <div class="mo-item-info">
    <div class="mo-item-name">{s['label']}</div>
    <div class="mo-item-sub">VTT Subtitle</div>
  </div>
  <i class="fa fa-check mo-item-check"></i>
</div>"""
    return html


# ══════════════════════════════════════════════════════════
#  BOT COMMAND /hls — Test command (optional)
# ══════════════════════════════════════════════════════════
@Client.on_message(filters.command("hls") & filters.reply)
async def hls_command(client: Client, message: Message):
    """Reply to a video with /hls to test HLS transcoding."""
    reply = message.reply_to_message
    media = reply.video or reply.document
    if not media:
        await message.reply("❌ Reply to a video file with /hls")
        return

    msg = await message.reply("⏳ Starting HLS transcode... this takes a moment")

    file_name = getattr(media, "file_name", None) or "video.mkv"
    ext       = Path(file_name).suffix or ".mkv"
    temp_path = f"/tmp/gf_hls_{media.file_id[:12]}{ext}"

    try:
        await msg.edit("📥 Downloading file...")
        await client.download_media(reply, file_name=temp_path)

        await msg.edit("🎬 Transcoding to HLS (multi-audio)...")
        result = await prepare_hls_stream(temp_path, media.file_id[:16])

        audio_list = "\n".join(
            [f"  {a['flag']} {a['label']}" for a in result["audio_tracks"]]
        ) or "  • Default"
        sub_list = "\n".join(
            [f"  {s['flag']} {s['label']}" for s in result["subtitle_urls"]]
        ) or "  • None"

        await msg.edit(
            f"✅ **HLS Stream Ready!**\n\n"
            f"🎵 **Audio Tracks:**\n{audio_list}\n\n"
            f"💬 **Subtitles:**\n{sub_list}\n\n"
            f"🔗 **Master Playlist:**\n`{result['master_url']}`"
        )
    except Exception as e:
        await msg.edit(f"❌ Error: `{e}`")
        logger.exception(e)
    finally:
        try:
            os.remove(temp_path)
        except Exception:
            pass
