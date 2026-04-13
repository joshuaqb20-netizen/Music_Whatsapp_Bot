import os
import uuid
import json
import subprocess
import threading
import shutil
import logging
from urllib.parse import quote
from flask import Flask, request, send_from_directory
from twilio.rest import Client
import imageio_ffmpeg
from googleapiclient.discovery import build

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

TWILIO_SID      = os.environ['TWILIO_ACCOUNT_SID']
TWILIO_TOKEN    = os.environ['TWILIO_AUTH_TOKEN']
PUBLIC_URL      = os.environ['RENDER_EXTERNAL_URL'].rstrip('/')
YOUTUBE_API_KEY = os.environ['YOUTUBE_API_KEY']
FFMPEG_PATH     = imageio_ffmpeg.get_ffmpeg_exe()

log.info(f"ffmpeg path resolved to: {FFMPEG_PATH}")
log.info(f"PUBLIC_URL: {PUBLIC_URL}")

# Write YouTube cookies from env var to a file for yt-dlp to use
COOKIES_PATH = os.path.join(os.path.dirname(__file__), 'cookies.txt')
youtube_cookies = os.environ.get('YOUTUBE_COOKIES', '')
if youtube_cookies:
    with open(COOKIES_PATH, 'w') as f:
        f.write(youtube_cookies)
    log.info(f"YouTube cookies written to: {COOKIES_PATH}")
else:
    COOKIES_PATH = None
    log.warning("No YOUTUBE_COOKIES env var found — downloads may fail with 429")

twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)
youtube       = build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)

DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), 'downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

user_sessions = {}

# ── File serving ──────────────────────────────────────────────────────────────

@app.route('/files/<job_id>/<path:filename>')
def serve_file(job_id, filename):
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    return send_from_directory(job_dir, filename)

# ── Helpers ───────────────────────────────────────────────────────────────────

def send_message(to, from_, body, media_url=None):
    kwargs = {'from_': from_, 'to': to, 'body': body}
    if media_url:
        kwargs['media_url'] = [media_url]
    twilio_client.messages.create(**kwargs)


def format_duration(iso_duration):
    """Convert ISO 8601 duration (PT4M13S) to mm:ss string."""
    import re
    if not iso_duration:
        return '?:??'
    match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', iso_duration)
    if not match:
        return '?:??'
    hours        = int(match.group(1) or 0)
    minutes      = int(match.group(2) or 0)
    seconds      = int(match.group(3) or 0)
    total_minutes = hours * 60 + minutes
    return f"{total_minutes}:{seconds:02d}"


def search_youtube(query):
    """
    Search YouTube using the Data API v3 — no bot detection, no JS runtime needed.
    Returns up to 5 results with title, channel, duration and URL.
    """
    log.info(f"[SEARCH] Starting YouTube API search for: '{query}'")

    # Step 1: search for video IDs
    search_response = youtube.search().list(
        q=query,
        part='id,snippet',
        maxResults=5,
        type='video'
    ).execute()

    items = search_response.get('items', [])
    log.info(f"[SEARCH] YouTube API returned {len(items)} item(s)")

    if not items:
        return []

    video_ids = [item['id']['videoId'] for item in items]
    log.debug(f"[SEARCH] Video IDs: {video_ids}")

    # Step 2: fetch durations for those video IDs
    details_response = youtube.videos().list(
        id=','.join(video_ids),
        part='contentDetails,snippet'
    ).execute()

    details_map = {}
    for video in details_response.get('items', []):
        vid_id = video['id']
        details_map[vid_id] = {
            'duration': format_duration(video['contentDetails']['duration']),
            'channel':  video['snippet']['channelTitle']
        }

    # Step 3: assemble results
    results = []
    for item in items:
        vid_id   = item['id']['videoId']
        title    = item['snippet']['title']
        details  = details_map.get(vid_id, {})
        channel  = details.get('channel', item['snippet']['channelTitle'])
        duration = details.get('duration', '?:??')
        url      = f"https://www.youtube.com/watch?v={vid_id}"

        log.debug(f"[SEARCH] Result: {title} — {channel} ({duration}) → {url}")
        results.append({
            'title':    title,
            'uploader': channel,
            'duration': duration,
            'url':      url,
        })

    log.info(f"[SEARCH] Successfully built {len(results)} result(s)")
    return results


def download_and_send(youtube_url, title, uploader, from_number, to_number):
    """Download a YouTube video as MP3 and send it back to the user."""
    job_id  = str(uuid.uuid4())
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    log.info(f"[DOWNLOAD] Starting download: {youtube_url}")

    try:
        output_template = os.path.join(job_dir, '%(title)s.%(ext)s')

        cmd = [
            'yt-dlp',
            youtube_url,
            '--extract-audio',
            '--audio-format', 'mp3',
            '--audio-quality', '0',
            '--output', output_template,
            '--no-playlist',
            '--ffmpeg-location', FFMPEG_PATH,
            '--extractor-args', 'youtube:player_client=tv,web',
            '--quiet'
        ]

        if COOKIES_PATH and os.path.exists(COOKIES_PATH):
            cmd += ['--cookies', COOKIES_PATH]
            log.debug(f"[DOWNLOAD] Using cookies from: {COOKIES_PATH}")
        else:
            log.warning("[DOWNLOAD] No cookies file — attempting download without authentication")

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)

        log.debug(f"[DOWNLOAD] yt-dlp return code: {result.returncode}")
        if result.stderr:
            log.warning(f"[DOWNLOAD] yt-dlp stderr: {result.stderr[:500]}")

        mp3_files = [f for f in os.listdir(job_dir) if f.endswith('.mp3')]

        if not mp3_files:
            error_detail = result.stderr or result.stdout or 'Unknown error'
            raise Exception(f"No MP3 created. yt-dlp said: {error_detail}")

        filename = mp3_files[0]
        file_url = f"{PUBLIC_URL}/files/{job_id}/{quote(filename)}"
        log.info(f"[DOWNLOAD] File ready at: {file_url}")

        send_message(
            from_number, to_number,
            f"✅ Here's *{title}* by *{uploader}*!",
            media_url=file_url
        )

        def cleanup():
            import time
            time.sleep(300)
            if os.path.exists(job_dir):
                shutil.rmtree(job_dir)

        threading.Thread(target=cleanup, daemon=True).start()

    except Exception as e:
        log.error(f"[DOWNLOAD] Failed for {youtube_url}: {e}", exc_info=True)
        if os.path.exists(job_dir):
            shutil.rmtree(job_dir)
        send_message(
            from_number, to_number,
            "❌ Download failed. The video may be unavailable or too large. "
            "Try a different result or search again."
        )


def handle_new_search(query, from_number, to_number):
    log.info(f"[HANDLER] handle_new_search called — query='{query}' from={from_number}")
    try:
        results = search_youtube(query)

        if not results:
            log.warning(f"[HANDLER] Search returned 0 results for '{query}'")
            send_message(
                from_number, to_number,
                f'❌ No results found for "{query}". Please try a different search.'
            )
            return

        user_sessions[from_number] = results
        log.info(f"[HANDLER] Session saved for {from_number} with {len(results)} result(s)")

        lines = [f'🔍 Top YouTube results for "{query}":\n']
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']} — {r['uploader']} ({r['duration']})")
        lines.append('\nReply with a number (1–5) to download, or send a new song name to search again.')

        send_message(from_number, to_number, '\n'.join(lines))
        log.info(f"[HANDLER] Results message sent to {from_number}")

    except Exception as e:
        log.error(f"[HANDLER] Search failed for '{query}': {e}", exc_info=True)
        send_message(from_number, to_number,
            "❌ Search failed. Please try again in a moment.")


# ── Webhook ───────────────────────────────────────────────────────────────────

@app.route('/webhook', methods=['POST'])
def webhook():
    from_number = request.form.get('From', '').strip()
    to_number   = request.form.get('To', '').strip()
    body        = request.form.get('Body', '').strip()

    log.info(f"[WEBHOOK] Incoming message from={from_number} body='{body}'")

    if not body or not from_number:
        return '', 200

    # ── Cancel ──────────────────────────────────────────────────────────────
    if body.lower() == 'cancel':
        if from_number in user_sessions:
            del user_sessions[from_number]
            send_message(from_number, to_number,
                "🚫 Search cancelled. Send a song name whenever you're ready.")
        else:
            send_message(from_number, to_number,
                "Nothing to cancel! Send a song name to search.")
        return '', 200

    # ── Direct YouTube URL ───────────────────────────────────────────────────
    if 'youtube.com/watch' in body or 'youtu.be/' in body:
        user_sessions.pop(from_number, None)
        send_message(from_number, to_number,
            "⬇️ Got your YouTube link! Downloading now, please wait...")
        threading.Thread(
            target=download_and_send,
            args=(body, 'your song', 'YouTube', from_number, to_number),
            daemon=True
        ).start()
        return '', 200

    # ── Number selection from previous search ────────────────────────────────
    if from_number in user_sessions and body in ('1', '2', '3', '4', '5'):
        idx     = int(body) - 1
        options = user_sessions[from_number]

        if idx >= len(options):
            send_message(from_number, to_number,
                f"⚠️ Please reply with a number between 1 and {len(options)}.")
            return '', 200

        chosen = options[idx]
        del user_sessions[from_number]

        send_message(from_number, to_number,
            f"⬇️ Downloading *{chosen['title']}* by *{chosen['uploader']}*...")

        threading.Thread(
            target=download_and_send,
            args=(chosen['url'], chosen['title'], chosen['uploader'], from_number, to_number),
            daemon=True
        ).start()

        return '', 200

    # ── New song search ──────────────────────────────────────────────────────
    user_sessions.pop(from_number, None)
    handle_new_search(body, from_number, to_number)
    return '', 200


# ── Health check ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return '🎵 Bot is running!'


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)
