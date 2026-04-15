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

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

TWILIO_SID   = os.environ['TWILIO_ACCOUNT_SID']
TWILIO_TOKEN = os.environ['TWILIO_AUTH_TOKEN']
PUBLIC_URL   = os.environ['RENDER_EXTERNAL_URL'].rstrip('/')
FFMPEG_PATH  = imageio_ffmpeg.get_ffmpeg_exe()

log.info(f"ffmpeg path resolved to: {FFMPEG_PATH}")
log.info(f"PUBLIC_URL: {PUBLIC_URL}")

twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)

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


def format_duration(seconds):
    if not seconds:
        return '?:??'
    seconds = int(float(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def search_soundcloud(query):
    """
    Use yt-dlp to search SoundCloud and return top 5 results.
    SoundCloud does not block datacenter IPs so this works on Render.
    """
    log.info(f"[SEARCH] Starting SoundCloud search for: '{query}'")

    cmd = [
        'yt-dlp',
        f'scsearch5:{query}',
        '--dump-json',
        '--no-download',
        '--no-playlist',
        '--quiet'
    ]

    log.debug(f"[SEARCH] Running command: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )
    except subprocess.TimeoutExpired:
        log.error("[SEARCH] yt-dlp search timed out after 30 seconds")
        raise Exception("Search timed out. Please try again.")
    except FileNotFoundError:
        log.error("[SEARCH] yt-dlp binary not found")
        raise Exception("yt-dlp not installed correctly.")

    log.debug(f"[SEARCH] yt-dlp return code: {result.returncode}")

    if result.stderr:
        log.warning(f"[SEARCH] yt-dlp stderr: {result.stderr[:300]}")

    lines = result.stdout.strip().splitlines()
    log.info(f"[SEARCH] yt-dlp returned {len(lines)} line(s)")

    results = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            info  = json.loads(line)
            title    = info.get('title', 'Unknown Title')
            uploader = info.get('uploader', 'Unknown Artist')
            duration = format_duration(info.get('duration'))
            url      = info.get('webpage_url') or info.get('url', '')

            log.debug(f"[SEARCH] Result {i+1}: {title} — {uploader} ({duration})")
            results.append({
                'title':    title,
                'uploader': uploader,
                'duration': duration,
                'url':      url,
            })
        except json.JSONDecodeError as e:
            log.error(f"[SEARCH] Failed to parse JSON on line {i}: {e}")
            continue

    log.info(f"[SEARCH] Successfully parsed {len(results)} result(s)")
    return results


def download_and_send(track_url, title, uploader, from_number, to_number):
    """Download a SoundCloud track as MP3 and send it to the user."""
    job_id  = str(uuid.uuid4())
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    log.info(f"[DOWNLOAD] Starting download: {track_url}")

    try:
        output_template = os.path.join(job_dir, '%(title)s.%(ext)s')

        cmd = [
            'yt-dlp',
            track_url,
            '--extract-audio',
            '--audio-format', 'mp3',
            '--audio-quality', '0',
            '--output', output_template,
            '--no-playlist',
            '--ffmpeg-location', FFMPEG_PATH,
            '--quiet'
        ]

        log.debug(f"[DOWNLOAD] Running command: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)

        log.debug(f"[DOWNLOAD] yt-dlp return code: {result.returncode}")
        if result.stderr:
            log.warning(f"[DOWNLOAD] yt-dlp stderr: {result.stderr[:500]}")

        mp3_files = [f for f in os.listdir(job_dir) if f.endswith('.mp3')]

        if not mp3_files:
            error_detail = result.stderr or result.stdout or 'Unknown error'
            raise Exception(f"No MP3 created. yt-dlp said: {error_detail}")

        filename = mp3_files[0]
        size_mb  = os.path.getsize(os.path.join(job_dir, filename)) / (1024 * 1024)
        log.info(f"[DOWNLOAD] MP3 ready — {filename} ({size_mb:.2f} MB)")

        if size_mb > 15:
            log.warning(f"[DOWNLOAD] File is {size_mb:.2f} MB — may exceed Twilio's 16MB limit")

        file_url = f"{PUBLIC_URL}/files/{job_id}/{quote(filename)}"
        log.info(f"[DOWNLOAD] Serving at: {file_url}")

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
        log.error(f"[DOWNLOAD] Failed for {track_url}: {e}", exc_info=True)
        if os.path.exists(job_dir):
            shutil.rmtree(job_dir)
        send_message(
            from_number, to_number,
            "❌ Download failed. The track may be unavailable. "
            "Try a different result or search again."
        )


def handle_new_search(query, from_number, to_number):
    log.info(f"[HANDLER] handle_new_search called — query='{query}' from={from_number}")
    try:
        results = search_soundcloud(query)

        if not results:
            log.warning(f"[HANDLER] Search returned 0 results for '{query}'")
            send_message(
                from_number, to_number,
                f'❌ No results found for "{query}". Please try a different search.'
            )
            return

        user_sessions[from_number] = results
        log.info(f"[HANDLER] Session saved for {from_number} with {len(results)} result(s)")

        lines = [f'🔍 Top SoundCloud results for "{query}":\n']
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

    # ── Direct SoundCloud URL ────────────────────────────────────────────────
    if 'soundcloud.com/' in body:
        user_sessions.pop(from_number, None)
        send_message(from_number, to_number,
            "⬇️ Got your SoundCloud link! Downloading now, please wait...")
        threading.Thread(
            target=download_and_send,
            args=(body, 'your track', 'SoundCloud', from_number, to_number),
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
