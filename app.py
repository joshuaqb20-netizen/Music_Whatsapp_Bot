import os
import uuid
import json
import subprocess
import threading
import shutil
from urllib.parse import quote
from flask import Flask, request, send_from_directory
from twilio.rest import Client

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

TWILIO_SID   = os.environ['TWILIO_ACCOUNT_SID']
TWILIO_TOKEN = os.environ['TWILIO_AUTH_TOKEN']
PUBLIC_URL   = os.environ['RENDER_EXTERNAL_URL'].rstrip('/')

twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)

DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), 'downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Stores pending search results per user: { phone_number: [result_dict, ...] }
user_sessions = {}

# ── File serving ──────────────────────────────────────────────────────────────

@app.route('/files/<job_id>/<path:filename>')
def serve_file(job_id, filename):
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    return send_from_directory(job_dir, filename)

# ── Helpers ───────────────────────────────────────────────────────────────────

def send_message(to, from_, body, media_url=None):
    """Send a WhatsApp message, optionally with a media attachment."""
    kwargs = {'from_': from_, 'to': to, 'body': body}
    if media_url:
        kwargs['media_url'] = [media_url]
    twilio_client.messages.create(**kwargs)


def format_duration(seconds):
    """Convert seconds to mm:ss string."""
    if not seconds:
        return '?:??'
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def search_youtube(query):
    """
    Use yt-dlp to search YouTube and return the top 5 results
    without downloading anything.
    """
    result = subprocess.run(
        [
            'yt-dlp',
            f'ytsearch5:{query}',
            '--dump-json',
            '--no-download',
            '--no-playlist',
            '--quiet'
        ],
        capture_output=True,
        text=True,
        timeout=30
    )

    if result.returncode != 0:
        raise Exception(f"yt-dlp search error: {result.stderr}")

    results = []
    for line in result.stdout.strip().splitlines():
        if not line.strip():
            continue
        try:
            info = json.loads(line)
            results.append({
                'title':    info.get('title', 'Unknown Title'),
                'uploader': info.get('uploader', 'Unknown Artist'),
                'duration': format_duration(info.get('duration')),
                'url':      info.get('webpage_url') or info.get('url', ''),
            })
        except json.JSONDecodeError:
            continue

    return results


def download_and_send(youtube_url, title, uploader, from_number, to_number):
    """Download a YouTube video as MP3 and send it back to the user."""
    job_id  = str(uuid.uuid4())
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        output_template = os.path.join(job_dir, '%(title)s.%(ext)s')

        result = subprocess.run(
            [
                'yt-dlp',
                youtube_url,
                '--extract-audio',
                '--audio-format', 'mp3',
                '--audio-quality', '0',
                '--output', output_template,
                '--no-playlist',
                '--quiet'
            ],
            capture_output=True,
            text=True,
            timeout=180
        )

        mp3_files = [f for f in os.listdir(job_dir) if f.endswith('.mp3')]

        if not mp3_files:
            error_detail = result.stderr or result.stdout or 'Unknown error'
            raise Exception(f"No MP3 created. yt-dlp said: {error_detail}")

        filename = mp3_files[0]
        file_url = f"{PUBLIC_URL}/files/{job_id}/{quote(filename)}"

        send_message(
            from_number, to_number,
            f"✅ Here's *{title}* by *{uploader}*!",
            media_url=file_url
        )

        # Clean up job folder after 5 minutes
        def cleanup():
            import time
            time.sleep(300)
            if os.path.exists(job_dir):
                shutil.rmtree(job_dir)

        threading.Thread(target=cleanup, daemon=True).start()

    except Exception as e:
        print(f"[ERROR] Download failed for {youtube_url}: {e}")
        if os.path.exists(job_dir):
            shutil.rmtree(job_dir)
        send_message(
            from_number, to_number,
            "❌ Download failed. The video may be unavailable or too large. "
            "Try a different result or search again."
        )


def handle_new_search(query, from_number, to_number):
    """Search YouTube and present the top 5 results to the user."""
    try:
        results = search_youtube(query)

        if not results:
            send_message(
                from_number, to_number,
                f'❌ No results found for "{query}". Please try a different search.'
            )
            return

        user_sessions[from_number] = results

        lines = [f'🔍 Top YouTube results for "{query}":\n']
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']} — {r['uploader']} ({r['duration']})")
        lines.append('\nReply with a number (1–5) to download, or send a new song name to search again.')

        send_message(from_number, to_number, '\n'.join(lines))

    except Exception as e:
        print(f"[ERROR] Search failed for '{query}': {e}")
        send_message(from_number, to_number,
            "❌ Search failed. Please try again in a moment.")


# ── Webhook ───────────────────────────────────────────────────────────────────

@app.route('/webhook', methods=['POST'])
def webhook():
    from_number = request.form.get('From', '').strip()
    to_number   = request.form.get('To', '').strip()
    body        = request.form.get('Body', '').strip()

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
    # Also clears any stale session so typing a new song always restarts
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
