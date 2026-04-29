import os
import re
import sys
import json
import time
import random
import threading
import subprocess
import queue
import glob as glob_module
import tempfile
import webbrowser
import shutil
from flask import Flask, render_template, request, Response, jsonify, send_file
import pandas as pd

app = Flask(__name__)

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
TRANSCRIPTS_DIR = os.path.join(BASE_DIR, 'generated_transcript_combined_texts')
METADATA_DIR    = os.path.join(BASE_DIR, 'generated_transcript_metadata_tables')

# Resolve yt-dlp: local exe > on PATH > same Python's Scripts dir
_local_ytdlp = os.path.join(BASE_DIR, 'yt-dlp.exe')
if os.path.exists(_local_ytdlp):
    YTDLP = _local_ytdlp
else:
    YTDLP = shutil.which('yt-dlp') or shutil.which('yt-dlp.exe')
    if not YTDLP:
        _scripts = os.path.join(os.path.dirname(sys.executable), 'Scripts', 'yt-dlp.exe')
        YTDLP = _scripts if os.path.exists(_scripts) else 'yt-dlp'

os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)
os.makedirs(METADATA_DIR,    exist_ok=True)

progress_queue = queue.Queue()
job_running    = False
job_lock       = threading.Lock()
cancel_event   = threading.Event()

MAX_RETRIES  = 3
RETRY_DELAYS = [15, 30, 60]   # seconds to wait between each retry attempt
VIDEO_DELAY  = (2.0, 5.0)     # random sleep range between videos


# ── helpers ───────────────────────────────────────────────────────────────────

def clean_filename(title):
    title = re.sub(r'[^\w\s-]', '', title)
    return re.sub(r'[-\s]+', '_', title).strip().lower()


def fetch_playlist_entries(url):
    result = subprocess.run(
        [YTDLP, '--flat-playlist', '--print', '%(id)s|||%(url)s|||%(title)s', url],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or 'yt-dlp failed to fetch playlist')
    entries = []
    for line in result.stdout.strip().splitlines():
        parts = line.split('|||', 2)
        if len(parts) == 3:
            vid, vurl, title = parts
            entries.append((vid.strip(), vurl.strip(), title.strip()))
    return entries


def parse_vtt(vtt_path):
    """Parse a WebVTT file into a list of {text, start, duration} dicts."""
    with open(vtt_path, 'r', encoding='utf-8') as f:
        content = f.read()
    data = []
    blocks = re.split(r'\n\n+', content.strip())
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 2:
            continue
        ts_match = None
        text_lines = []
        for j, line in enumerate(lines):
            # VTT timestamps use . not , and may have positioning info after the times
            m = re.match(
                r'(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s+-->\s+(\d{2}):(\d{2}):(\d{2})\.(\d{3})',
                line
            )
            if m:
                ts_match = m
                text_lines = lines[j + 1:]
                break
        if not ts_match or not text_lines:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = [int(x) for x in ts_match.groups()]
        start = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000
        end   = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000
        text  = ' '.join(text_lines)
        # Strip word-level timing tags e.g. <00:00:01.500> and <c>, </c>
        text  = re.sub(r'<\d{2}:\d{2}:\d{2}\.\d{3}>', '', text)
        text  = re.sub(r'<[^>]+>', '', text)
        text  = re.sub(r'\s+', ' ', text).strip()
        if text:
            data.append({
                'text':     text,
                'start':    round(start, 2),
                'duration': round(max(end - start, 0), 2),
            })
    # Deduplicate consecutive identical entries (YouTube auto-captions repeat lines)
    deduped = []
    for entry in data:
        if not deduped or entry['text'] != deduped[-1]['text']:
            deduped.append(entry)
    return deduped


def get_transcript(video_url, browser=None):
    """
    Fetch subtitles via yt-dlp (downloads YouTube's own captions as VTT).
    Returns (data, source) on success or (None, reason_str) on failure.
    browser: None | 'chrome' | 'firefox'
    """
    base_cmd = [
        YTDLP,
        '--write-auto-subs',
        '--write-subs',
        '--skip-download',
        '--sub-langs', 'en.*',
        '--sub-format', 'vtt',
        '--no-playlist',
    ]

    attempts = []
    if browser:
        attempts.append(base_cmd + ['--cookies-from-browser', browser])
    attempts.append(base_cmd)

    for cmd_base in attempts:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cmd = cmd_base + ['-o', os.path.join(tmp_dir, '%(id)s.%(ext)s'), video_url]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            except subprocess.TimeoutExpired:
                return None, 'timeout'

            stderr       = result.stderr
            stderr_lower = stderr.lower()

            # If this was a cookies attempt and the browser cookie read failed for
            # any reason (DPAPI, locked DB, profile not found, etc.), fall back to
            # the no-cookies attempt rather than failing every video.
            is_cookies_attempt = '--cookies-from-browser' in cmd
            cookie_read_failed = any(kw in stderr_lower for kw in (
                'dpapi', 'failed to decrypt', 'unable to read', 'could not find',
                'database is locked', 'no cookies', 'keyring', 'cookies from browser',
            ))
            if is_cookies_attempt and cookie_read_failed:
                continue

            vtt_files = glob_module.glob(os.path.join(tmp_dir, '*.vtt'))

            if not vtt_files:
                if '429' in stderr or 'too many requests' in stderr_lower:
                    return None, 'rate_limited'
                if 'private' in stderr_lower:
                    return None, 'private video'
                if 'unavailable' in stderr_lower or 'removed' in stderr_lower:
                    return None, 'video unavailable'
                if 'sign in' in stderr_lower or 'confirm your age' in stderr_lower or 'age-restricted' in stderr_lower:
                    return None, 'age-restricted — re-run with a browser selected in the cookies dropdown'
                short_err = (stderr or 'no output').strip().splitlines()[-1][:120]
                return None, f'no captions — {short_err}'

            data = parse_vtt(vtt_files[0])
            if not data:
                return None, 'empty transcript'
            return data, 'auto-generated'

    return None, 'no captions available'


def save_transcript(data, filename, title, video_url, source):
    combined = ' '.join(e['text'] for e in data)
    combined = re.sub(r'\s+', ' ', combined).strip()

    txt_path = os.path.join(TRANSCRIPTS_DIR, f'{filename}.txt')
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write(f"Title: {title}\nURL: {video_url}\nSource: {source}\n")
        f.write('=' * 60 + '\n\n')
        f.write(combined)

    df = pd.DataFrame(data)
    df.to_csv(os.path.join(METADATA_DIR, f'{filename}.csv'), index=False)
    df.to_json(os.path.join(METADATA_DIR, f'{filename}.json'), orient='records', indent=4)
    return txt_path


def emit(event, data):
    progress_queue.put({'event': event, 'data': data})


# ── background job ────────────────────────────────────────────────────────────

def run_job(url, browser):
    global job_running
    consecutive_rate_limits = 0

    try:
        emit('status', {'msg': 'Fetching video list…'})
        entries = fetch_playlist_entries(url)
        total   = len(entries)
        if total == 0:
            emit('error', {'msg': 'No videos found at that URL.'})
            return
        emit('total', {'total': total})

        success, skipped = 0, 0

        for i, (video_id, video_url, title) in enumerate(entries, 1):
            if cancel_event.is_set():
                emit('cancelled', {'success': success, 'skipped': skipped})
                return

            filename = clean_filename(title) or video_id
            txt_path = os.path.join(TRANSCRIPTS_DIR, f'{filename}.txt')

            emit('progress', {'i': i, 'total': total, 'title': title, 'state': 'working'})

            if os.path.exists(txt_path):
                emit('progress', {'i': i, 'total': total, 'title': title,
                                  'state': 'skipped', 'reason': 'already exists'})
                success += 1
                consecutive_rate_limits = 0
                continue

            data, source = None, None
            for attempt in range(MAX_RETRIES):
                if cancel_event.is_set():
                    break
                yt_url = f'https://www.youtube.com/watch?v={video_id}'
                data, source = get_transcript(yt_url, browser)
                if data:
                    consecutive_rate_limits = 0
                    break
                if source == 'rate_limited':
                    consecutive_rate_limits += 1
                    wait = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                    if consecutive_rate_limits > 3:
                        wait = min(wait * 2, 120)
                    emit('progress', {'i': i, 'total': total, 'title': title,
                                      'state': 'retrying', 'attempt': attempt + 1,
                                      'wait': wait})
                    time.sleep(wait)
                else:
                    break   # permanent error, don't retry

            if cancel_event.is_set():
                emit('cancelled', {'success': success, 'skipped': skipped})
                return

            if data:
                save_transcript(data, filename, title, yt_url, source)
                emit('progress', {'i': i, 'total': total, 'title': title,
                                  'state': 'done', 'source': source, 'filename': filename})
                success += 1
            else:
                emit('progress', {'i': i, 'total': total, 'title': title,
                                  'state': 'failed', 'reason': source})
                skipped += 1

            time.sleep(random.uniform(*VIDEO_DELAY))

        emit('done', {'success': success, 'skipped': skipped, 'out_dir': TRANSCRIPTS_DIR})

    except Exception as e:
        emit('error', {'msg': str(e)})
    finally:
        with job_lock:
            job_running = False


# ── routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/start', methods=['POST'])
def start():
    global job_running

    body    = request.get_json(silent=True) or {}
    url     = (body.get('url') or '').strip()
    browser = body.get('browser') or None

    if not url or ('youtube.com' not in url and 'youtu.be' not in url):
        return jsonify({'error': 'Please enter a valid YouTube URL.'}), 400

    with job_lock:
        if job_running:
            return jsonify({'error': 'A job is already running.'}), 400
        job_running = True

    # Drain any stale events from a previous run
    while not progress_queue.empty():
        try:
            progress_queue.get_nowait()
        except queue.Empty:
            break
    cancel_event.clear()

    try:
        threading.Thread(target=run_job, args=(url, browser), daemon=True).start()
    except Exception as e:
        with job_lock:
            job_running = False
        return jsonify({'error': f'Failed to start job: {e}'}), 500

    return jsonify({'ok': True})


@app.route('/cancel', methods=['POST'])
def cancel():
    cancel_event.set()
    return jsonify({'ok': True})


@app.route('/stream')
def stream():
    def generate():
        idle_pings = 0
        while True:
            try:
                item = progress_queue.get(timeout=15)
                idle_pings = 0
                yield f"event: {item['event']}\ndata: {json.dumps(item['data'])}\n\n"
                if item['event'] in ('done', 'error', 'cancelled'):
                    break
            except queue.Empty:
                # If no job is running and the queue stays empty, close the
                # stream rather than pinging forever.
                if not job_running:
                    yield "event: closed\ndata: {}\n\n"
                    break
                idle_pings += 1
                # Hard cap as a final safety net (~1 hour of idle pings).
                if idle_pings > 240:
                    yield "event: error\ndata: {\"msg\": \"Stream idle timeout.\"}\n\n"
                    break
                yield "event: ping\ndata: {}\n\n"
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/download/<filename>')
def download(filename):
    # Prevent path traversal: reject anything that isn't a plain basename.
    if not filename or filename != os.path.basename(filename):
        return 'Invalid filename', 400
    if not re.fullmatch(r'[\w\-]+', filename):
        return 'Invalid filename', 400

    path = os.path.normpath(os.path.join(TRANSCRIPTS_DIR, filename + '.txt'))
    if os.path.commonpath([os.path.abspath(path), TRANSCRIPTS_DIR]) != TRANSCRIPTS_DIR:
        return 'Invalid filename', 400
    if not os.path.exists(path):
        return 'File not found', 404
    return send_file(path, as_attachment=True)


@app.route('/transcripts')
def list_transcripts():
    files = []
    if not os.path.isdir(TRANSCRIPTS_DIR):
        return jsonify(files)
    for f in sorted(os.listdir(TRANSCRIPTS_DIR)):
        if not f.endswith('.txt'):
            continue
        name = f[:-4]
        try:
            size = os.path.getsize(os.path.join(TRANSCRIPTS_DIR, f))
        except OSError:
            continue
        files.append({'name': name, 'size_kb': round(size / 1024, 1)})
    return jsonify(files)


if __name__ == '__main__':
    webbrowser.open('http://127.0.0.1:5000')
    app.run(debug=False, threaded=True)
