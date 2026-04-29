# YouTube Playlist Transcriber

A tool for bulk-downloading transcripts from YouTube videos and playlists. Three separate entry points exist, each with different capabilities and dependency requirements.

## How to Run

```bash
cd "C:\Users\Ruben\Documents\Claude\YouTube-playlist-transcriber"
python app.py
```

Opens browser automatically at `http://127.0.0.1:5000`. Flask caches templates with `debug=False`, so **restart the server after every change to `app.py` or `templates/index.html`**.

To kill the server between sessions:
```powershell
$pids = (netstat -ano | findstr ":5000 " | Select-String "LISTENING" | ForEach-Object { ($_ -split '\s+')[-1] } | Sort-Object -Unique)
foreach ($p in $pids) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }
```

## Entry Points

### `app.py` — Flask Web App (primary)
- Uses `yt-dlp` to fetch YouTube's built-in VTT captions (no audio download, very fast)
- Handles single videos and playlists
- Browser cookies option (Chrome/Firefox) for age-restricted videos
- Real-time progress via Server-Sent Events (`/stream` endpoint)
- Retry logic: up to 3 retries, delays of 15/30/60s; back-off doubles after 3 consecutive rate limits
- Random 2–5s delay between videos to avoid rate limiting
- Skips videos whose `.txt` output already exists
- Cancel button stops the background thread mid-job
- `/transcripts` lists saved files; `/download/<filename>` serves them
- `/config` GET/POST — reads and writes `config.json` (output folder setting)
- `/browse` — opens a native Windows folder picker (tkinter) and returns the chosen path

### `transcribe.py` — CLI Script
- Run: `python transcribe.py` → prompts for URL
- Uses `youtube_transcript_api` library (not `yt-dlp`) — faster but less control
- Simple one-retry on rate limit (5s wait), random 2–4s delay between videos
- Lighter dependencies

### `bulk_transcribe_youtube_videos_from_playlist.py` — Legacy Heavy Script
- **Not used by the web app.** Standalone script for when YouTube captions aren't available.
- Downloads actual audio via `pytubefix`, then transcribes with local Whisper (`faster_whisper`) or OpenAI Whisper API
- GPU support via CUDA; auto-detects and falls back to CPU
- Configuration is hardcoded at the top of the file (URLs, flags, API key)
- Heavy deps: `pytubefix`, `faster_whisper`, `numba`, `pydub`, `tqdm`, `openai`, `psutil`, optional `spacy`
- These deps are **not** in `requirements.txt`

## Output

Output location is configurable via the UI. The chosen folder is stored in `config.json` in the project root. Inside the chosen folder, two subdirectories are created automatically:

| Directory | Contents |
|---|---|
| `generated_transcript_combined_texts/` | `<filename>.txt` — header + plain combined transcript text |
| `generated_transcript_metadata_tables/` | `<filename>.csv` and `<filename>.json` — per-segment timing data |

Default (if no folder is configured): project root directory.

**Text file format:**
```
Title: <video title>
URL: <video url>
Source: auto-generated
============================================================

<full transcript as one block of text>
```

**Segment metadata fields:** `text`, `start` (seconds), `duration` (seconds)

**Filename convention:** `clean_filename()` strips non-word chars, lowercases, replaces spaces/hyphens with `_`. Falls back to `video_id` if title cleans to empty.

## Dependencies

Install for `app.py` and `transcribe.py`:
```
pip install -r requirements.txt  # flask, pandas, yt-dlp
```

**yt-dlp resolution order** (in `app.py`):
1. `yt-dlp.exe` in the project root
2. `yt-dlp` on the system PATH (via `shutil.which`)
3. `Scripts/yt-dlp.exe` next to the current Python executable (where pip installs it)

pip on this machine installs scripts to `C:\Users\Ruben\AppData\Local\Python\pythoncore-3.14-64\Scripts\` which is **not** on PATH — the fallback in step 3 handles this.

## Known Limitations

- **Age-restricted videos**: require browser cookies. Select Chrome or Firefox in the dropdown before starting. Chrome 127+ uses App-Bound Encryption that yt-dlp can't decrypt — selecting Chrome behaves the same as None. Firefox works if the profile is accessible and Firefox is not locking the cookie DB.
- **Browser cookie failures**: any cookie-read error (locked DB, profile not found, DPAPI) silently falls back to no-cookies so non-restricted videos still work.

## Architecture Notes (`app.py`)

- `job_running` flag + `job_lock` (`threading.Lock`) — check/set atomically to prevent two simultaneous jobs
- `progress_queue` (`queue.Queue`) — background thread pushes events; `/stream` SSE endpoint drains it
- `cancel_event` (`threading.Event`) — set by `/cancel`; checked between videos in the job loop
- SSE stream closes itself when idle with no job running (within 15s); hard cap at ~1 hour
- `get_dirs()` — reads `config.json`, resolves output folder via `os.path.abspath()`, creates subdirs, returns `(transcripts_dir, metadata_dir)`. Called once per job at start; also called by `/download` and `/transcripts`.
- `/download/<filename>` — validates against `[\w\-]+` regex + `commonpath` containment check (both sides `abspath`'d) to prevent path traversal. Path normalization ensures forward-slash paths from tkinter don't break the check on Windows.
- `config.json` — persists `output_dir`. Paths are stored normalized via `os.path.normpath()`.

## UI (`templates/index.html`)

Single-page app. Key behaviours:
- **Save to** row — folder path input + Browse button. Browse opens a native Windows folder picker. Typing a path and tabbing away saves it. Setting persists in `config.json` across restarts.
- **Clear button** (next to status line) — appears after a job finishes; wipes the log, progress bar, and banners from the dashboard without deleting any files
- **Previously saved transcripts** section — shown on fresh page load only; does **not** reappear after a job completes (the job log already has download buttons). Has its own Clear button that hides the list and persists the hidden state in `localStorage`.
- **Done banner** — shows the actual save path (`out_dir` from the SSE `done` event), not a hardcoded folder name.
- EventSource error handling: named `error` events from the server show a message and reset buttons; transport-level failures reset buttons only if `readyState === CLOSED`

## Standalone Tools

### `transcript_reader.html`
A standalone browser tool (open directly, not served by Flask) for cleaning and reading raw transcripts. Paste or upload a `.txt` file, uses `compromise.js` to split into sentences and format into paragraphs. Has a full-screen reader mode with adjustable font, size, and width.

## Build

`build.bat` + `YouTube Transcriber.spec` — PyInstaller config for packaging the web app as a Windows `.exe`.
