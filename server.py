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

PORT = int(os.environ.get("PORT", sys.argv[1] if len(sys.argv) > 1 else 7860))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

LYRICS_CACHE = {}
DOWNLOAD_LOCK = threading.Lock()
ACTIVE_DOWNLOADS = set()

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
            'noplaylist': True
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
        # Try clean query first
        try:
            lrc = syncedlyrics.search(clean_q, synced_only=True)
        except Exception as e:
            print(f"[Lyrics Error for '{clean_q}']: {e}")

        # If not found and clean_q differs significantly from raw query, try raw query
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
                        'format': 'bestaudio[ext=m4a]/bestaudio/best',
                        'outtmpl': os.path.join(CACHE_DIR, f"{vid_id}.%(ext)s"),
                        'quiet': True,
                        'noplaylist': True,
                    }
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([f"https://www.youtube.com/watch?v={vid_id}"])
                except Exception as e:
                    print(f"[Download Error for {vid_id}]: {e}")
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
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Audio could not be downloaded")
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
