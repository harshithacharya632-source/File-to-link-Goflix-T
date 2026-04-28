import jinja2
import aiofiles
import os
import json
import asyncio
import subprocess
import urllib.parse
import logging
import aiohttp
from web.utils.Template import rexbots_template
from info import *
from web.server import StreamBot
from utils import get_size
from web.utils.file_properties import get_file_ids
from web.server.exceptions import InvalidHash

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
    "chi":"Chinese","zh":"Chinese","zho":"Chinese",
    "rus":"Russian","ru":"Russian","por":"Portuguese","pt":"Portuguese",
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
#  FFPROBE — probe tracks directly from stream URL
# ══════════════════════════════════════════════════════════
def probe_tracks(url: str) -> dict:
    """
    Run ffprobe on the stream URL — no download needed.
    Reads only metadata (very fast, ~2-5 seconds).
    """
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-user_agent", "Mozilla/5.0",
        url
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        data   = json.loads(result.stdout)
    except Exception as e:
        logging.warning(f"[GoFlix ffprobe] {e}")
        return {"audio": [], "subtitles": []}

    audio = []
    subs  = []

    for s in data.get("streams", []):
        kind  = s.get("codec_type", "")
        idx   = s.get("index", 0)
        tags  = s.get("tags", {})
        lang  = (tags.get("language") or tags.get("LANGUAGE") or "und").lower().strip()
        title = tags.get("title") or tags.get("TITLE") or ""
        codec = s.get("codec_name", "").upper()
        label = title if title else LANG_NAMES.get(lang, lang.upper())
        flag  = LANG_FLAGS.get(lang, "🎵" if kind=="audio" else "💬")

        if kind == "audio":
            ch   = s.get("channels", 2)
            ch_s = {1:"Mono",2:"Stereo",6:"5.1",8:"7.1"}.get(ch, f"{ch}ch")
            audio.append({"index":idx,"label":label,"lang":lang,"codec":codec,"channels":ch_s,"flag":flag})

        elif kind == "subtitle":
            subs.append({"index":idx,"label":label,"lang":lang,"codec":codec,"flag":flag})

    logging.info(f"[GoFlix] Tracks — audio:{len(audio)} subtitles:{len(subs)}")
    return {"audio": audio, "subtitles": subs}


# ══════════════════════════════════════════════════════════
#  HTML BUILDERS
# ══════════════════════════════════════════════════════════
def build_audio_html(tracks: list) -> str:
    if not tracks:
        return """<div class="dp-item active">
  <div class="dp-flag">🎵</div>
  <div class="dp-info">
    <div class="dp-name">Default Audio</div>
    <div class="dp-sub">Original track</div>
  </div>
  <i class="fa fa-check dp-check"></i>
</div>"""
    html = ""
    for i, t in enumerate(tracks):
        active = "active" if i == 0 else ""
        html += f"""<div class="dp-item {active}">
  <div class="dp-flag">{t['flag']}</div>
  <div class="dp-info">
    <div class="dp-name">{t['label']}</div>
    <div class="dp-sub">{t['codec']} &bull; {t['channels']}</div>
  </div>
  <i class="fa fa-check dp-check"></i>
</div>"""
    return html


def build_subtitle_html(tracks: list) -> str:
    # Off item is always added in the HTML template itself
    # This only returns the track items (not Off)
    if not tracks:
        return ""
    html = ""
    for t in tracks:
        html += f"""<div class="dp-item">
  <div class="dp-flag">{t['flag']}</div>
  <div class="dp-info">
    <div class="dp-name">{t['label']}</div>
    <div class="dp-sub">{t['codec']}</div>
  </div>
  <i class="fa fa-check dp-check"></i>
</div>"""
    return html


# ══════════════════════════════════════════════════════════
#  MAIN render_page  (drop-in replacement — same signature)
# ══════════════════════════════════════════════════════════
async def render_page(id: str, secure_hash: str, src: str = None) -> str:

    # Step 1: Fetch Telegram file and metadata
    try:
        file      = await StreamBot.get_messages(int(BIN_CHANNEL), int(id))
        file_data = await get_file_ids(StreamBot, int(BIN_CHANNEL), int(id))
    except Exception as e:
        logging.error(f"Error fetching file info: {e}")
        raise

    # Step 2: Validate secure_hash
    if file_data.unique_id[:6] != secure_hash:
        raise InvalidHash

    # Step 3: Construct file URL
    url_base = URL if URL.endswith("/") else URL + "/"
    src = urllib.parse.urljoin(url_base, f"{id}?hash={secure_hash}")

    # Step 4: Determine file type and size
    tag       = file_data.mime_type.split("/")[0].strip()
    file_size = get_size(file_data.file_size)

    if tag in ["video", "audio"]:
        template_file = os.path.join("web", "template", "watch.html")
    else:
        template_file = os.path.join("web", "template", "dl.html")
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(src) as u:
                    if u.status == 200:
                        cl = u.headers.get("Content-Length")
                        file_size = get_size(int(cl)) if cl else "Unknown"
                    else:
                        file_size = "Unknown"
        except Exception as e:
            file_size = "Unknown"

    # Step 5: Read template
    try:
        async with aiofiles.open(template_file, mode='r') as f:
            content = await f.read()
        template = jinja2.Template(content)
    except Exception as e:
        logging.error(f"Error reading template: {e}")
        return "Template Error"

    # Step 6: File name + Telegram link
    file_name = file_data.file_name.replace("_", " ") if file_data.file_name else f"File_{id}.mkv"
    tg_link   = f"https://t.me/{BOT_USERNAME}?start=file_{id}"

    # ══════════════════════════════════════════════════
    #  ★ PROBE AUDIO & SUBTITLE TRACKS via ffprobe ★
    #  Runs in thread — does NOT block async event loop
    #  Only runs for video/audio files
    # ══════════════════════════════════════════════════
    audio_html    = ""
    subtitle_html = ""
    audio_json    = "[]"
    subtitle_json = "[]"
    audio_count   = 0
    subtitle_count = 0

    if tag in ["video", "audio"]:
        try:
            loop   = asyncio.get_event_loop()
            tracks = await asyncio.wait_for(
                loop.run_in_executor(None, probe_tracks, src),
                timeout=22
            )
            audio_html     = build_audio_html(tracks["audio"])
            subtitle_html  = build_subtitle_html(tracks["subtitles"])
            audio_json     = json.dumps(tracks["audio"],      ensure_ascii=False)
            subtitle_json  = json.dumps(tracks["subtitles"],  ensure_ascii=False)
            audio_count    = len(tracks["audio"]) or 1
            subtitle_count = len(tracks["subtitles"])
        except asyncio.TimeoutError:
            logging.warning(f"[GoFlix] ffprobe timeout for {src}")
            audio_html  = build_audio_html([])   # shows "Default Audio"
            audio_count = 1
        except Exception as e:
            logging.warning(f"[GoFlix] ffprobe error: {e}")
            audio_html  = build_audio_html([])
            audio_count = 1

    # Step 7: Render
    return template.render(
        # ── Original variables (unchanged) ──
        file_name       = file_name,
        file_url        = src,
        file_size       = file_size,
        file_unique_id  = file_data.unique_id,
        template_ne     = rexbots_template.NAME,
        disclaimer      = rexbots_template.DISCLAIMER,
        report_link     = rexbots_template.REPORT_LINK,
        colours         = rexbots_template.COLOURS,
        tg_button       = tg_link,
        # ── NEW: audio & subtitle track data ──
        audio_items_html    = audio_html,
        subtitle_items_html = subtitle_html,
        audio_tracks_json   = audio_json,
        subtitle_tracks_json= subtitle_json,
        audio_count         = audio_count,
        subtitle_count      = subtitle_count,
    )
