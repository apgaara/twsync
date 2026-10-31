#!/usr/bin/env python3
import os
import re
import sys
import glob
import json
import time
import urllib.parse
import threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import yt_dlp
import syncedlyrics
from syncedlyrics.providers import NetEase

import tempfile

PORT = int(os.environ.get("PORT", sys.argv[1] if len(sys.argv) > 1 else 10000))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.environ.get("CACHE_DIR", os.path.join(tempfile.gettempdir(), "tws_cache"))
os.makedirs(CACHE_DIR, exist_ok=True)
COOKIES_FILE = os.path.join(BASE_DIR, "cookies.txt")

INVIDIOUS_INSTANCES = [
    'https://yt.chocolatemoo53.com',
    'https://invidious.nerdvpn.de',
    'https://inv.nadeko.net',
    'https://invidious.tiekoetter.com'
]

def fallback_invidious_download(vid_id, out_file):
    for inst in INVIDIOUS_INSTANCES:
        try:
            r = requests.get(f'{inst}/api/v1/videos/{vid_id}', timeout=5)
            if r.status_code == 200:
                data = r.json()
                audio_formats = [f for f in data.get('adaptiveFormats', []) if 'audio' in f.get('type', '')]
                if not audio_formats:
                    continue
                best_audio = sorted(audio_formats, key=lambda x: int(x.get('bitrate', 0)), reverse=True)[0]
                stream_url = best_audio.get('url')
                if not stream_url:
                    continue
                with requests.get(stream_url, stream=True, timeout=15) as s:
                    s.raise_for_status()
                    with open(out_file, 'wb') as f:
                        for chunk in s.iter_content(chunk_size=65536):
                            if chunk:
                                f.write(chunk)
                if os.path.exists(out_file) and os.path.getsize(out_file) > 10000:
                    return True
        except Exception:
            continue
    return False

LYRICS_CACHE = {}
DOWNLOAD_LOCK = threading.Lock()
ACTIVE_DOWNLOADS = set()
NETEASE_INSTANCE = NetEase()

def fetch_smart_netease_lyrics(query):
    clean_q = clean_title_for_lyrics(query)
    words = clean_q.split()
    if not words:
        return None
    
    # Try query permutations (Title first, Artist second, etc.)
    permutations = [clean_q]
    if len(words) >= 3:
        permutations.append(' '.join(words[2:] + words[:2]))
        permutations.append(' '.join(words[2:]))
        permutations.append(' '.join(words[1:] + words[:1]))
        permutations.append(' '.join(words[1:]))

    for p in permutations:
        try:
            params = {'limit': 15, 'type': 1, 'offset': 0, 's': p}
            res = NETEASE_INSTANCE.session.get(NETEASE_INSTANCE.API_ENDPOINT_METADATA, params=params).json()
            songs = res.get('result', {}).get('songs', [])
            
            for s in songs:
                s_name = s.get('name', '').lower()
                s_artists = ' '.join(a.get('name', '').lower() for a in s.get('artists', []))
                
                # Check if significant words in clean_q match this song
                q_words = [w.lower() for w in words if len(w) > 2]
                all_match = all(w in f'{s_name} {s_artists}' for w in q_words)
                
                if all_match:
                    lrc_obj = NETEASE_INSTANCE.get_lrc_by_id(s.get('id'))
                    if lrc_obj and lrc_obj.synced:
                        return lrc_obj.synced
        except Exception:
            continue
            
    return None

def clean_channel_name(ch):
    if not ch:
        return ""
    cleaned = re.sub(r'[\-_]\s*topic\b', '', ch, flags=re.IGNORECASE)
    cleaned = re.sub(r'\b(topic)\b', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'vevo$', '', cleaned, flags=re.IGNORECASE)
    return cleaned.strip()

def clean_title_for_lyrics(title):
    cleaned = re.sub(r'[\(\[\{][^\)\]\}]*(?:official|video|audio|lyric|mv|remastered|4k|hd|live|ft\.|feat\.|clip|visualizer)[^\)\]\}]*[\)\]\}]', '', title, flags=re.IGNORECASE)
    cleaned = re.sub(r'\b(official|music video|lyric video|audio|remastered|4k|hd|visualizer)\b', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'[-–—_|/]', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

def parse_lrc(lrc_text):
    if not lrc_text:
        return []
    lines = []
    time_regex = re.compile(r'\[(\d{2}):(\d{2})\.?(\d{2,3})?\]')
    credit_keywords = ['作词', '作曲', '作詞', 'lyricist', 'composer', 'written by', 'produced by']

    for line in lrc_text.splitlines():
        match = time_regex.search(line)
        if match:
            minute = int(match.group(1))
            sec = int(match.group(2))
            ms_str = match.group(3) or '0'
            ms = int(ms_str.ljust(3, '0')[:3])
            t = minute * 60 + sec + ms / 1000.0
            txt = time_regex.sub('', line).strip()
            
            if not txt:
                continue
            # Filter credits only in first 3 seconds
            if t < 3.0 and any(kw in txt.lower() for kw in credit_keywords):
                continue
                
            lines.append({'time': round(t, 2), 'text': txt})

    lines.sort(key=lambda x: x['time'])
    return lines

def format_duration(seconds):
    if not seconds:
        return "--:--"
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m:02d}:{s:02d}"

class TWSLyricRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Range, Content-Type')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/audio":
            params = urllib.parse.parse_qs(parsed.query)
            vid_id = params.get('id', [''])[0].strip()
            matches = [f for f in glob.glob(os.path.join(CACHE_DIR, f"{vid_id}.*")) if not f.endswith('.part')]
            if matches and os.path.exists(matches[0]):
                audio_file = matches[0]
                ext = os.path.splitext(audio_file)[1].lower()
                content_type = 'audio/mp4' if ext in ['.m4a', '.mp4'] else ('audio/webm' if ext == '.webm' else 'audio/mpeg')
                file_size = os.path.getsize(audio_file)
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(file_size))
                self.send_header('Accept-Ranges', 'bytes')
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()
            return
        super().do_HEAD()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path == "/api/search":
            self.handle_api_search(params)
        elif path == "/api/lyrics":
            self.handle_api_lyrics(params)
        elif path == "/api/audio":
            self.handle_api_audio(params)
        elif path == "/api/cache_status":
            self.handle_cache_status(params)
        elif path == "/api/version":
            self.send_json({"version": "1.3-pot-provider", "status": "ok", "has_cookies": os.path.exists(COOKIES_FILE)})
        else:
            # Serve static files
            super().do_GET()

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_api_search(self, params):
        q = params.get('q', [''])[0].strip()
        if not q:
            self.send_json({"error": "Empty query", "results": []}, 400)
            return

        ydl_opts = {
            'extract_flat': 'in_playlist',
            'skip_download': True,
            'quiet': True,
            'noplaylist': True,
            'extractor_args': {
                'youtube': {
                    'player_client': ['android'],
                }
            }
        }

        results = []
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch8:{q}", download=False)
                for entry in info.get('entries', []):
                    if not entry:
                        continue
                    vid_id = entry.get('id')
                    raw_title = entry.get('title', '')
                    cleaned = clean_title_for_lyrics(raw_title)
                    dur = entry.get('duration')
                    # Check if already cached
                    cached = bool(glob.glob(os.path.join(CACHE_DIR, f"{vid_id}.*")))

                    results.append({
                        "id": vid_id,
                        "title": raw_title,
                        "clean_title": cleaned,
                        "channel": entry.get('channel') or entry.get('uploader') or "",
                        "duration": dur,
                        "duration_str": format_duration(dur),
                        "thumbnail": f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg",
                        "is_cached": cached
                    })
            self.send_json({"results": results})
        except Exception as e:
            self.send_json({"error": str(e), "results": []}, 500)

    def handle_api_lyrics(self, params):
        q = params.get('q', [''])[0].strip()
        artist = params.get('artist', [''])[0].strip()
        
        if artist:
            clean_art = clean_channel_name(artist)
            if clean_art and clean_art.lower() not in q.lower():
                q = f"{q} {clean_art}"

        if not q:
            self.send_json({"found": False, "lines": [], "lrc": ""}, 400)
            return

        clean_q = clean_title_for_lyrics(q)
        cache_key = clean_q.lower()

        if cache_key in LYRICS_CACHE:
            cached_lrc = LYRICS_CACHE[cache_key]
            lines = parse_lrc(cached_lrc)
            self.send_json({"found": bool(lines), "lines": lines, "lrc": cached_lrc or "", "query": clean_q, "cached": True})
            return

        lrc = None
        # 1. Try smart NetEase matcher with title/artist permutations
        try:
            lrc = fetch_smart_netease_lyrics(clean_q)
        except Exception as e:
            print(f"[Smart NetEase Error for '{clean_q}']: {e}")

        # 2. Try syncedlyrics multi-provider fallback
        if not lrc:
            try:
                lrc = syncedlyrics.search(clean_q, synced_only=True)
            except Exception as e:
                print(f"[Lyrics Error for '{clean_q}']: {e}")

        # 3. If still not found, try raw query
        if not lrc and clean_q != q:
            try:
                lrc = syncedlyrics.search(q, synced_only=True)
            except Exception as e:
                print(f"[Lyrics Error for '{q}']: {e}")

        if lrc:
            LYRICS_CACHE[cache_key] = lrc
            lines = parse_lrc(lrc)
            self.send_json({"found": bool(lines), "lines": lines, "lrc": lrc, "query": clean_q})
        else:
            LYRICS_CACHE[cache_key] = ""
            self.send_json({"found": False, "lines": [], "lrc": "", "query": clean_q})

    def handle_cache_status(self, params):
        vid_id = params.get('id', [''])[0].strip()
        matches = glob.glob(os.path.join(CACHE_DIR, f"{vid_id}.*"))
        ready = len(matches) > 0 and not any(m.endswith('.part') for m in matches)
        self.send_json({"ready": ready})

    def handle_api_audio(self, params):
        vid_id = params.get('id', [''])[0].strip()
        if not vid_id or not re.match(r'^[a-zA-Z0-9_-]{11}$', vid_id):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Invalid Video ID")
            return

        # Check existing cached audio
        matches = [f for f in glob.glob(os.path.join(CACHE_DIR, f"{vid_id}.*")) if not f.endswith('.part')]
        audio_file = matches[0] if matches else None

        dl_error_msg = ""
        if not audio_file:
            # Need to download
            with DOWNLOAD_LOCK:
                if vid_id not in ACTIVE_DOWNLOADS:
                    ACTIVE_DOWNLOADS.add(vid_id)
                    needs_dl = True
                else:
                    needs_dl = False

            if needs_dl:
                try:
                    ydl_opts = {
                        'format': 'bestaudio/18/best',
                        'outtmpl': os.path.join(CACHE_DIR, f"{vid_id}.%(ext)s"),
                        'quiet': True,
                        'noplaylist': True,
                        'source_address': '0.0.0.0',
                        'extractor_args': {
                            'youtube': {
                                'player_client': ['android'],
                            }
                        }
                    }
                    if os.path.exists(COOKIES_FILE):
                        ydl_opts['cookiefile'] = COOKIES_FILE
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([f"https://www.youtube.com/watch?v={vid_id}"])
                except Exception as e:
                    dl_error_msg = str(e)
                    print(f"[Download Error for {vid_id} via yt-dlp]: {e}")
                    # Invidious proxy fallback
                    fallback_file = os.path.join(CACHE_DIR, f"{vid_id}.m4a")
                    try:
                        if fallback_invidious_download(vid_id, fallback_file):
                            print(f"[Fallback Invidious Download Success for {vid_id}]")
                    except Exception as fe:
                        print(f"[Fallback error for {vid_id}]: {fe}")
                finally:
                    with DOWNLOAD_LOCK:
                        ACTIVE_DOWNLOADS.discard(vid_id)
            else:
                # Wait for existing download to finish
                while vid_id in ACTIVE_DOWNLOADS:
                    time.sleep(0.3)

            matches = [f for f in glob.glob(os.path.join(CACHE_DIR, f"{vid_id}.*")) if not f.endswith('.part')]
            if matches:
                audio_file = matches[0]

        if not audio_file or not os.path.exists(audio_file):
            self.send_response(500)
            self.end_headers()
            msg = f"Audio download failed: {dl_error_msg}" if dl_error_msg else "Audio could not be downloaded"
            self.wfile.write(msg.encode())
            return

        # Serve audio with Range header support
        ext = os.path.splitext(audio_file)[1].lower()
        content_type = 'audio/mp4' if ext in ['.m4a', '.mp4'] else ('audio/webm' if ext == '.webm' else 'audio/mpeg')

        file_size = os.path.getsize(audio_file)
        range_header = self.headers.get('Range')

        if range_header and range_header.startswith('bytes='):
            range_val = range_header.split('=')[1].strip()
            parts = range_val.split('-')
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if len(parts) > 1 and parts[1] else file_size - 1
            if start >= file_size:
                start = file_size - 1
            if end >= file_size:
                end = file_size - 1

            chunk_len = (end - start) + 1
            self.send_response(206)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Range', f"bytes {start}-{end}/{file_size}")
            self.send_header('Content-Length', str(chunk_len))
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()

            with open(audio_file, 'rb') as f:
                f.seek(start)
                remaining = chunk_len
                while remaining > 0:
                    read_size = min(remaining, 65536)
                    buf = f.read(read_size)
                    if not buf:
                        break
                    try:
                        self.wfile.write(buf)
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    remaining -= len(buf)
        else:
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(file_size))
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()

            with open(audio_file, 'rb') as f:
                while True:
                    buf = f.read(65536)
                    if not buf:
                        break
                    try:
                        self.wfile.write(buf)
                    except (BrokenPipeError, ConnectionResetError):
                        break

def run_server():
    server = ThreadingHTTPServer(('0.0.0.0', PORT), TWSLyricRequestHandler)
    print(f"🚀 TWS Hack Python Backend running at http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == '__main__':
    run_server()
