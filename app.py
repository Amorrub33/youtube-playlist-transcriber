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
from flask import Flask, render_template, request, Response, jsonify, send_file
import pandas as pd

app = Flask(__name__)

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
TRANSCRIPTS_DIR = os.path.join(BASE_DIR, 'generated_transcript_combined_texts')
METADATA_DIR    = os.path.join(BASE_DIR, 'generated_transcript_metadata_tables')

# Use local yt-dlp.exe if present, otherwise fall back to system yt-dlp
_local_ytdlp = os.path.join(BASE_DIR, 'yt-dlp.exe')
YTDLP = _local_ytdlp if os.path.exists(_local_ytdlp) else 'yt-dlp'

os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)
os.makedirs(METADATA_DIR,    exist_ok=True)

progress_queue = queue.Queue()
job_running    = False
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

    # Build list of attempts: with cookies first (if requested), then without
    attempts = []
    if browser:
        attempts.append(base_cmd + ['--cookies-from-browser', browser])
    attempts.append(base_cmd)  # always fall back to no cookies

    for cmd_base in attempts:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cmd = cmd_base + ['-o', os.path.join(tmp_dir, '%(id)s.%(ext)s'), video_url]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            except subprocess.TimeoutExpired:
                return None, 'timeout'

            stderr       = result.stderr
            stderr_lower = stderr.lower()

            # If cookies failed due to DPAPI/decryption, skip to no-cookies attempt
            if 'dpapi' in stderr_lower or 'failed to decrypt' in stderr_lower:
                continue

            vtt_files = glob_module.glob(os.path.join(tmp_dir, '*.vtt'))

            if not vtt_files:
                if '429' in stderr or 'too many requests' in stderr_lower:
                    return None, 'rate_limited'
                if 'private' in stderr_lower:
                    return None, 'private video'
                if 'unavailable' in stderr_lower or 'removed' in stderr_lower:
                    return None, 'video unavailable'
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
    job_running = True
    cancel_event.clear()
    consecutive_rate_limits = 0

    try:
        emit('status', {'msg': 'Fetching video list…'})
        entries = fetch_playlist_entries(url)
        total   = len(entries)
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
        job_running = False


# ── routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/start', methods=['POST'])
def start():
    global job_running
    if job_running:
        return jsonify({'error': 'A job is already running.'}), 400

    body    = request.json or {}
    url     = body.get('url', '').strip()
    browser = body.get('browser') or None   # 'chrome', 'firefox', or None

    if not url or ('youtube.com' not in url and 'youtu.be' not in url):
        return jsonify({'error': 'Please enter a valid YouTube URL.'}), 400

    while not progress_queue.empty():
        progress_queue.get_nowait()

    threading.Thread(target=run_job, args=(url, browser), daemon=True).start()
    return jsonify({'ok': True})


@app.route('/cancel', methods=['POST'])
def cancel():
    cancel_event.set()
    return jsonify({'ok': True})


@app.route('/stream')
def stream():
    def generate():
        while True:
            try:
                item = progress_queue.get(timeout=30)
                yield f"event: {item['event']}\ndata: {json.dumps(item['data'])}\n\n"
                if item['event'] in ('done', 'error', 'cancelled'):
                    break
            except queue.Empty:
                yield "event: ping\ndata: {}\n\n"
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/download/<filename>')
def download(filename):
    path = os.path.join(TRANSCRIPTS_DIR, filename + '.txt')
    if os.path.exists(path):
        return send_file(path, as_attachment=True)
    return 'File not found', 404


@app.route('/transcripts')
def list_transcripts():
    files = []
    for f in sorted(os.listdir(TRANSCRIPTS_DIR)):
        if f.endswith('.txt'):
            name = f[:-4]
            size = os.path.getsize(os.path.join(TRANSCRIPTS_DIR, f))
            files.append({'name': name, 'size_kb': round(size / 1024, 1)})
    return jsonify(files)


if __name__ == '__main__':
    webbrowser.open('http://127.0.0.1:5000')
    app.run(debug=False, threaded=True)
