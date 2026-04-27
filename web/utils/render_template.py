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
#  FFPROBE — Get audio & subtitle tracks from streaming URL
# ══════════════════════════════════════════════════════════
def probe_tracks_from_url(file_url: str) -> dict:
    """
    Run ffprobe directly on the Telegram stream URL.
    No download needed — ffprobe reads just the header/metadata.
    Returns { "audio": [...], "subtitles": [...] }
    """
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-user_agent", "Mozilla/5.0",  # Some servers need this
        file_url
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data   = json.loads(result.stdout)
    except Exception as e:
        logging.warning(f"[GoFlix ffprobe] Could not probe: {e}")
        return {"audio": [], "subtitles": []}

    audio     = []
    subtitles = []

    for s in data.get("streams", []):
        kind  = s.get("codec_type", "")
        idx   = s.get("index", 0)
        tags  = s.get("tags", {})
        lang  = (
            tags.get("language") or tags.get("LANGUAGE") or "und"
        ).lower().strip()
        title = tags.get("title") or tags.get("TITLE") or ""
        codec = s.get("codec_name", "").upper()
        label = title if title else LANG_NAMES.get(lang, lang.upper())
        flag  = LANG_FLAGS.get(lang, "🎵" if kind == "audio" else "💬")

        if kind == "audio":
            ch   = s.get("channels", 2)
            ch_s = {1:"Mono",2:"Stereo",6:"5.1",8:"7.1"}.get(ch, f"{ch}ch")
            audio.append({
                "index":    idx,
                "label":    label,
                "lang":     lang,
                "codec":    codec,
                "channels": ch_s,
                "flag":     flag,
            })

        elif kind == "subtitle":
            subtitles.append({
                "index": idx,
                "label": label,
                "lang":  lang,
                "codec": codec,
                "flag":  flag,
            })

    logging.info(f"[GoFlix] Found {len(audio)} audio, {len(subtitles)} subtitle tracks")
    return {"audio": audio, "subtitles": subtitles}


# ══════════════════════════════════════════════════════════
#  HTML BUILDERS — for audio & subtitle panels
# ══════════════════════════════════════════════════════════
def build_audio_html(tracks: list) -> str:
    """Build HTML items for audio panel."""
    if not tracks:
        return '<div class="mo-empty"><i class="fa fa-headphones"></i><br>Default Audio Only</div>'
    html = ""
    for i, t in enumerate(tracks):
        active = "active" if i == 0 else ""
        html += f"""<div class="mo-item {active}">
  <div class="mo-item-icon">{t['flag']}</div>
  <div class="mo-item-info">
    <div class="mo-item-name">{t['label']}</div>
    <div class="mo-item-sub">{t['codec']} &bull; {t['channels']}</div>
  </div>
  <i class="fa fa-check mo-item-check"></i>
</div>"""
    return html


def build_subtitle_html(tracks: list) -> str:
    """Build HTML items for subtitle panel. Off is always first."""
    html = """<div class="mo-item active" id="subOffItem">
  <div class="mo-item-icon">🚫</div>
  <div class="mo-item-info">
    <div class="mo-item-name">Off</div>
    <div class="mo-item-sub">No subtitles</div>
  </div>
  <i class="fa fa-check mo-item-check"></i>
</div>"""
    for t in tracks:
        html += f"""<div class="mo-item">
  <div class="mo-item-icon">{t['flag']}</div>
  <div class="mo-item-info">
    <div class="mo-item-name">{t['label']}</div>
    <div class="mo-item-sub">{t['codec']}</div>
  </div>
  <i class="fa fa-check mo-item-check"></i>
</div>"""
    return html


# ══════════════════════════════════════════════════════════
#  MAIN render_page FUNCTION  (drop-in replacement)
# ══════════════════════════════════════════════════════════
async def render_page(id: str, secure_hash: str, src: str = None) -> str:

    # Step 1: Fetch Telegram file and metadata
    try:
        file = await StreamBot.get_messages(int(BIN_CHANNEL), int(id))
        file_data = await get_file_ids(StreamBot, int(BIN_CHANNEL), int(id))
    except Exception as e:
        logging.error(f"Error fetching file info: {e}")
        raise

    # Step 2: Validate secure_hash
    if file_data.unique_id[:6] != secure_hash:
        logging.debug(f"link hash: {secure_hash} - {file_data.unique_id[:6]}")
        logging.debug(f"Invalid hash for message with - ID {id}")
        raise InvalidHash

    # Step 3: Construct file URL
    if not URL.endswith("/"):
        url_base = URL + "/"
    else:
        url_base = URL
    src = urllib.parse.urljoin(url_base, f"{id}?hash={secure_hash}")

    # Step 4: Determine file tag and get size
    tag = file_data.mime_type.split("/")[0].strip()
    file_size = get_size(file_data.file_size)

    if tag in ["video", "audio"]:
        template_file = os.path.join("web", "template", "watch.html")
    else:
        template_file = os.path.join("web", "template", "dl.html")
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(src) as u:
                    if u.status == 200:
                        content_length = u.headers.get("Content-Length")
                        file_size = get_size(int(content_length)) if content_length else "Unknown"
                    else:
                        file_size = "Unknown"
        except Exception as e:
            logging.error(f"Failed to fetch file size from URL: {e}")
            file_size = "Unknown"

    # Step 5: Read template file
    try:
        async with aiofiles.open(template_file, mode='r') as f:
            content = await f.read()
        template = jinja2.Template(content)
    except Exception as e:
        logging.error(f"Error reading template: {e}")
        return "Template Error"

    # Step 6: Prepare file name
    file_name = file_data.file_name.replace("_", " ") if file_data.file_name else f"File_{id}.mkv"
    tg_link   = f"https://t.me/{BOT_USERNAME}?start=file_{id}"

    # ══════════════════════════════════════════════════════
    #  ★ NEW: Probe audio & subtitle tracks via ffprobe ★
    #  Only for video/audio files — runs in thread so it
    #  doesn't block the async event loop
    # ══════════════════════════════════════════════════════
    audio_html    = ""
    subtitle_html = ""
    audio_json    = "[]"
    subtitle_json = "[]"

    if tag in ["video", "audio"]:
        try:
            loop   = asyncio.get_event_loop()
            tracks = await asyncio.wait_for(
                loop.run_in_executor(None, probe_tracks_from_url, src),
                timeout=25  # max 25 seconds to probe
            )
            audio_html    = build_audio_html(tracks["audio"])
            subtitle_html = build_subtitle_html(tracks["subtitles"])
            audio_json    = json.dumps(tracks["audio"],     ensure_ascii=False)
            subtitle_json = json.dumps(tracks["subtitles"], ensure_ascii=False)
        except asyncio.TimeoutError:
            logging.warning(f"[GoFlix] ffprobe timed out for {src}")
        except Exception as e:
            logging.warning(f"[GoFlix] ffprobe error: {e}")
        # Even if ffprobe fails, page still loads normally
        # — audio/subtitle panels will show "Default Audio Only"

    # Step 7: Render template with all values
    return template.render(
        # ── Original variables (unchanged) ──
        file_name=file_name,
        file_url=src,
        file_size=file_size,
        file_unique_id=file_data.unique_id,
        template_ne=rexbots_template.NAME,
        disclaimer=rexbots_template.DISCLAIMER,
        report_link=rexbots_template.REPORT_LINK,
        colours=rexbots_template.COLOURS,
        tg_button=tg_link,
        # ── NEW variables for audio/subtitle panels ──
        audio_items_html=audio_html,
        subtitle_items_html=subtitle_html,
        audio_tracks_json=audio_json,
        subtitle_tracks_json=subtitle_json,
        audio_count=len(json.loads(audio_json)),
        subtitle_count=len(json.loads(subtitle_json)),
    )
