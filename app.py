import os
import uuid
import threading
import shutil
import logging
import requests
from urllib.parse import quote
from flask import Flask, request, send_from_directory
from twilio.rest import Client
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
RAPIDAPI_KEY    = os.environ['RAPIDAPI_KEY']

log.info(f"PUBLIC_URL: {PUBLIC_URL}")

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
    import re
    if not iso_duration:
        return '?:??'
    match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', iso_duration)
    if not match:
        return '?:??'
    hours         = int(match.group(1) or 0)
    minutes       = int(match.group(2) or 0)
    seconds       = int(match.group(3) or 0)
    total_minutes = hours * 60 + minutes
    return f"{total_minutes}:{seconds:02d}"


def search_youtube(query):
    log.info(f"[SEARCH] Starting YouTube API search for: '{query}'")

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
    import time

    job_id  = str(uuid.uuid4())
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    log.info(f"[DOWNLOAD] Starting API download for: {youtube_url}")

    try:
        # ── Step 1: Call RapidAPI to convert the YouTube URL to an MP3 link ──
        log.debug("[DOWNLOAD] Calling RapidAPI conversion endpoint...")

        api_response = requests.get(
            "https://youtube-mp310.p.rapidapi.com/download/mp3",
            headers={
                "x-rapidapi-key":  RAPIDAPI_KEY,
                "x-rapidapi-host": "youtube-mp310.p.rapidapi.com"
            },
            params={
                "url": youtube_url
            },
            timeout=60
        )

        log.debug(f"[DOWNLOAD] RapidAPI status code: {api_response.status_code}")
        log.debug(f"[DOWNLOAD] RapidAPI raw response: {api_response.text[:500]}")

        if api_response.status_code != 200:
            raise Exception(f"RapidAPI returned status {api_response.status_code}: {api_response.text}")

        data = api_response.json()
        log.debug(f"[DOWNLOAD] RapidAPI parsed response: {data}")

        # ── Step 2: Extract the MP3 download link from the response ──
        mp3_url = data.get('downloadUrl')

        if not mp3_url:
            raise Exception(f"No downloadUrl in response: {data}")

        log.info(f"[DOWNLOAD] Got MP3 URL from API: {mp3_url[:80]}...")

        # ── Step 3: Download the MP3 to Render's disk ──
        filename     = f"{job_id}.mp3"
        filepath     = os.path.join(job_dir, filename)

        log.debug("[DOWNLOAD] Downloading MP3 file to disk...")
        mp3_response = requests.get(mp3_url, timeout=120)

        if mp3_response.status_code != 200:
            raise Exception(f"Failed to download MP3 file: status {mp3_response.status_code}")

        with open(filepath, 'wb') as f:
            f.write(mp3_response.content)

        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        log.info(f"[DOWNLOAD] MP3 saved to disk — size: {size_mb:.2f} MB")

        if size_mb > 15:
            log.warning(f"[DOWNLOAD] File is {size_mb:.2f} MB — may exceed Twilio's 16MB limit")

        # ── Step 4: Serve the file and send it via WhatsApp ──
        file_url = f"{PUBLIC_URL}/files/{job_id}/{filename}"
        log.info(f"[DOWNLOAD] Serving file at: {file_url}")

        send_message(
            from_number, to_number,
            f"✅ Here's *{title}* by *{uploader}*!",
            media_url=file_url
        )

        # Clean up after 5 minutes
        def cleanup():
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
            "❌ Download failed. Try a different result or search again."
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

    if body.lower() == 'cancel':
        if from_number in user_sessions:
            del user_sessions[from_number]
            send_message(from_number, to_number,
                "🚫 Search cancelled. Send a song name whenever you're ready.")
        else:
            send_message(from_number, to_number,
                "Nothing to cancel! Send a song name to search.")
        return '', 200

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
