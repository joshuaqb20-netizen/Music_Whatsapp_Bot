import os
import uuid
import subprocess
import threading
import shutil
from urllib.parse import quote
from flask import Flask, request, send_from_directory
from twilio.rest import Client
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

TWILIO_SID            = os.environ['TWILIO_ACCOUNT_SID']
TWILIO_TOKEN          = os.environ['TWILIO_AUTH_TOKEN']
PUBLIC_URL            = os.environ['RENDER_EXTERNAL_URL'].rstrip('/')
SPOTIFY_CLIENT_ID     = os.environ['SPOTIFY_CLIENT_ID']
SPOTIFY_CLIENT_SECRET = os.environ['SPOTIFY_CLIENT_SECRET']

twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)

# ✅ UPDATED Spotify init with error handling + test
try:
    auth_manager = SpotifyClientCredentials(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET
    )
    sp = spotipy.Spotify(auth_manager=auth_manager)

    # 🔥 Force test call
    sp.search(q="test", type="track", limit=1)
    print("✅ Spotify auth working")

except Exception as e:
    print("❌ Spotify INIT FAILED:", e)
    sp = None

DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), 'downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Stores pending search results per user: { phone_number: [track_dict, ...] }
user_sessions = {}

# ── File serving ──────────────────────────────────────────────────────────────

@app.route('/files/<job_id>/<path:filename>')
def serve_file(job_id, filename):
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    return send_from_directory(job_dir, filename)

# ── Helpers ───────────────────────────────────────────────────────────────────

def send_message(to, from_, body, media_url=None):
    """Send a WhatsApp message, optionally with a media file."""
    kwargs = {'from_': from_, 'to': to, 'body': body}
    if media_url:
        kwargs['media_url'] = [media_url]
    twilio_client.messages.create(**kwargs)


def search_spotify(query):
    # ✅ Added safety check
    if not sp:
        raise Exception("Spotify not initialized")

    """Search Spotify and return up to 5 track results."""
    results = sp.search(q=query, type='track', limit=5)
    tracks = results['tracks']['items']
    options = []
    for track in tracks:
        name    = track['name']
        artists = ', '.join(a['name'] for a in track['artists'])
        url     = track['external_urls']['spotify']
        ms      = track['duration_ms']
        duration = f"{ms // 60000}:{(ms % 60000) // 1000:02d}"
        options.append({
            'name': name,
            'artists': artists,
            'url': url,
            'duration': duration
        })
    return options


def download_and_send(spotify_url, track_name, artists, from_number, to_number):
    """Download a song via spotdl and send it back to the user."""
    job_id  = str(uuid.uuid4())
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        output_template = os.path.join(job_dir, '{artists} - {title}.mp3')

        result = subprocess.run(
            [
                'spotdl', spotify_url,
                '--output', output_template,
                '--format', 'mp3'
            ],
            capture_output=True,
            text=True,
            timeout=300  # ✅ increased timeout
        )

        # ✅ Added debug logs
        print("SPOTDL STDOUT:", result.stdout)
        print("SPOTDL STDERR:", result.stderr)

        mp3_files = [f for f in os.listdir(job_dir) if f.endswith('.mp3')]

        if not mp3_files:
            error_detail = result.stderr or result.stdout or 'Unknown error'
            raise Exception(f"No MP3 created. spotdl said: {error_detail}")

        filename = mp3_files[0]
        file_url = f"{PUBLIC_URL}/files/{job_id}/{quote(filename)}"

        send_message(
            from_number, to_number,
            f"✅ Here's *{track_name}* by *{artists}*!",
            media_url=file_url
        )

        # Clean up the job folder after 5 minutes
        def cleanup():
            import time
            time.sleep(300)
            if os.path.exists(job_dir):
                shutil.rmtree(job_dir)

        threading.Thread(target=cleanup, daemon=True).start()

    except Exception as e:
        print(f"[ERROR] Download failed for {spotify_url}: {e}")
        if os.path.exists(job_dir):
            shutil.rmtree(job_dir)
        send_message(
            from_number, to_number,
            f"❌ Download failed: {str(e)}"  # ✅ improved error message
        )


def handle_new_search(query, from_number, to_number):
    """Search Spotify and present the top 5 results to the user."""
    try:
        options = search_spotify(query)

        if not options:
            send_message(
                from_number, to_number,
                f'❌ No results found for "{query}". Please try a different search.'
            )
            return

        user_sessions[from_number] = options

        lines = [f'🔍 Top results for "{query}":\n']
        for i, opt in enumerate(options, 1):
            lines.append(f"{i}. {opt['name']} — {opt['artists']} ({opt['duration']})")
        lines.append('\nReply with a number to download, or send a new song name to search again.')

        send_message(from_number, to_number, '\n'.join(lines))

    except Exception as e:
        print(f"[ERROR] Search failed for '{query}': {repr(e)}")  # ✅ better logging
        send_message(
            from_number, to_number,
            f"❌ Search failed: {str(e)}"  # ✅ improved error message
        )

# ── Webhook ───────────────────────────────────────────────────────────────────

@app.route('/webhook', methods=['POST'])
def webhook():
    from_number = request.form.get('From', '').strip()
    to_number   = request.form.get('To', '').strip()
    body        = request.form.get('Body', '').strip()

    if not body or not from_number:
        return '', 200

    # ── Cancel ──
    if body.lower() == 'cancel':
        if from_number in user_sessions:
            del user_sessions[from_number]
            send_message(from_number, to_number,
                "🚫 Search cancelled. Send a song name whenever you're ready.")
        else:
            send_message(from_number, to_number,
                "Nothing to cancel! Send a song name to search.")
        return '', 200

    # ── Direct Spotify URL ──
    if body.startswith('https://open.spotify.com/track/'):
        try:
            track_id   = body.split('/track/')[1].split('?')[0]
            track      = sp.track(track_id)
            track_name = track['name']
            artists    = ', '.join(a['name'] for a in track['artists'])

            # Clear any pending session
            user_sessions.pop(from_number, None)

            send_message(from_number, to_number,
                f"⬇️ Downloading *{track_name}* by *{artists}*...")

            threading.Thread(
                target=download_and_send,
                args=(body, track_name, artists, from_number, to_number),
                daemon=True
            ).start()

        except Exception as e:
            print(f"[ERROR] Spotify URL lookup failed: {e}")
            send_message(from_number, to_number,
                "❌ Couldn't read that Spotify link. Try searching by song name instead.")
        return '', 200

    # ── Selection from previous search ──
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
            f"⬇️ Downloading *{chosen['name']}* by *{chosen['artists']}*...")

        threading.Thread(
            target=download_and_send,
            args=(chosen['url'], chosen['name'], chosen['artists'], from_number, to_number),
            daemon=True
        ).start()

        return '', 200

    # ── New song search (also clears any stale session) ──
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
